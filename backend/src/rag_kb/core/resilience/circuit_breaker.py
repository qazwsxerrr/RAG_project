"""src/rag_kb/core/resilience/circuit_breaker.py —— 模型健康追踪器、SLA滑动窗口与熔断状态机。

维护状态：
- CLOSED: 正常健康，放行全部请求
- OPEN: 熔断隔离，阻断请求直接漂移备用模型
- HALF_OPEN: 冷却期过后，单请求探测锁试探自愈，其余请求继续走备用节点
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

@dataclass
class CallMetric:
    timestamp: float
    success: bool
    duration_ms: int
    ttft_ms: Optional[int]
    error_category: Optional[str]

class ModelHealthTracker:
    """每个模型节点的健康状态机与 SLA 滑动窗口指标统计器"""

    def __init__(
        self,
        window_size: int = 100,
        window_seconds: int = 300,
        cooldown_seconds: int = 300,
        error_threshold_percent: float = 5.0,
        min_calls: int = 5,
        consecutive_failures_threshold: int = 3
    ):
        self.window_size = window_size
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self.error_threshold_percent = error_threshold_percent
        self.min_calls = min_calls
        self.consecutive_failures_threshold = consecutive_failures_threshold

        # 节点指标双向队列: node_name -> deque[CallMetric]
        self._metrics: Dict[str, deque[CallMetric]] = {}
        # 节点熔断状态: node_name -> "CLOSED" | "OPEN" | "HALF_OPEN"
        self._state: Dict[str, str] = {}
        # 熔断触发时间: node_name -> float
        self._tripped_at: Dict[str, float] = {}
        # 上次告警时间: node_name -> float (防止告警风暴)
        self._last_alert_at: Dict[str, float] = {}
        # 管理员手动重置时间戳: node_name -> float
        self._reset_at: Dict[str, float] = {}
        # 突发连续失败计数: node_name -> int
        self._consecutive_failures: Dict[str, int] = {}
        # HALF_OPEN 单请求探测锁: node_name -> bool
        self._probe_in_flight: Dict[str, bool] = {}
        # 是否已完成冷启动回填: node_name -> bool
        self._hydrated: Dict[str, bool] = {}

    def _ensure_node(self, node_name: str):
        if node_name not in self._metrics:
            self._metrics[node_name] = deque(maxlen=self.window_size)
            self._state[node_name] = "CLOSED"
            self._consecutive_failures[node_name] = 0
            self._probe_in_flight[node_name] = False

    def _prune_expired(self, node_name: str, now: Optional[float] = None):
        """双维窗口裁剪：剔除超过时间窗口或早于最后重置时间的样本"""
        self._ensure_node(node_name)
        curr_time = now if now is not None else time.time()
        reset_time = self._reset_at.get(node_name, 0.0)
        cutoff_time = max(curr_time - self.window_seconds, reset_time)

        q = self._metrics[node_name]
        while q and q[0].timestamp < cutoff_time:
            q.popleft()

    def get_node_state(self, node_name: str) -> str:
        """获取节点当前熔断状态并驱动 HALF_OPEN 流转"""
        self._ensure_node(node_name)
        state = self._state.get(node_name, "CLOSED")
        if state == "OPEN":
            tripped_time = self._tripped_at.get(node_name, 0.0)
            if time.time() - tripped_time >= self.cooldown_seconds:
                self._state[node_name] = "HALF_OPEN"
                self._probe_in_flight[node_name] = False
                return "HALF_OPEN"
        return state

    def acquire_execution_permit(self, node_name: str) -> bool:
        """
        防惊群试探控制：获取当前节点的执行许可
        - CLOSED: 放行全部并发请求
        - OPEN: 阻断全部请求
        - HALF_OPEN: 仅放行唯一 1 个并发协程作为探测流量，其余请求返回 False 导流至备用池
        """
        state = self.get_node_state(node_name)
        if state == "CLOSED":
            return True
        if state == "OPEN":
            return False
        if state == "HALF_OPEN":
            if self._probe_in_flight.get(node_name, False):
                # 探测请求已在执行中，拒绝后续并发，防止半开惊群
                return False
            self._probe_in_flight[node_name] = True
            return True
        return False

    def release_execution_permit(self, node_name: str):
        """释放探测锁（在请求未完成即异常中断时保障状态不被死锁）"""
        self._ensure_node(node_name)
        self._probe_in_flight[node_name] = False

    def is_node_available(self, node_name: str) -> bool:
        """检查节点名义可用性 (供只读大盘与连通性判断)"""
        state = self.get_node_state(node_name)
        return state in ("CLOSED", "HALF_OPEN")

    def record_success(self, node_name: str, duration_ms: int = 0, ttft_ms: Optional[int] = None):
        """记录成功调用并自愈 HALF_OPEN 节点"""
        self._ensure_node(node_name)
        self._prune_expired(node_name)
        self._metrics[node_name].append(CallMetric(time.time(), True, duration_ms, ttft_ms, None))
        self._consecutive_failures[node_name] = 0
        self._probe_in_flight[node_name] = False

        if self._state.get(node_name) in ("HALF_OPEN", "OPEN"):
            self._state[node_name] = "CLOSED"
            self._tripped_at.pop(node_name, None)

    def record_failure(
        self,
        node_name: str,
        duration_ms: int = 0,
        err_category: str = "ERR_UNKNOWN",
        immediate_trip: bool = False
    ):
        """
        记录失败调用并根据四大跳闸条件评估是否熔断：
        1. 业务安全拦截与客户端参数错误不计入节点硬件故障；
        2. 致命凭据失效 (ERR_UPSTREAM_AUTH) 或账户欠费耗尽 (immediate_trip=True) 0 等待立即跳闸；
        3. 连续失败达到阈值 (consecutive_failures >= 3) 快速跳闸；
        4. 滑动窗口内样本 >= min_calls 且错误率 >= 阈值 (5%) 跳闸。
        """
        # 业务合规阻断与请求参数错误不计入节点健康度
        if err_category in ("ERR_SECURITY", "ERR_BAD_REQUEST"):
            self._probe_in_flight[node_name] = False
            return

        self._ensure_node(node_name)
        self._prune_expired(node_name)
        self._metrics[node_name].append(CallMetric(time.time(), False, duration_ms, None, err_category))
        self._consecutive_failures[node_name] = self._consecutive_failures.get(node_name, 0) + 1

        # 若处于 HALF_OPEN 试探失败，立即重新置为 OPEN 并重新计时冷却
        if self._state.get(node_name) == "HALF_OPEN":
            self._state[node_name] = "OPEN"
            self._tripped_at[node_name] = time.time()
            self._probe_in_flight[node_name] = False
            return

        # 条件 1: 致命凭据故障 (401/403 Key 禁用) 或确定性账户欠费 (429 insufficient_quota) 单次立即跳闸
        if immediate_trip or err_category == "ERR_UPSTREAM_AUTH":
            self._state[node_name] = "OPEN"
            self._tripped_at[node_name] = time.time()
            return

        # 条件 2: 突发连续失败快速跳闸
        if self._consecutive_failures[node_name] >= self.consecutive_failures_threshold:
            self._state[node_name] = "OPEN"
            self._tripped_at[node_name] = time.time()
            return

        # 条件 3: 窗口内有效样本数达到 min_calls 且错误率超过阈值
        error_rate, _, _ = self.get_sla_stats(node_name)
        sample_count = len(self._metrics[node_name])
        if sample_count >= self.min_calls and error_rate >= self.error_threshold_percent:
            self._state[node_name] = "OPEN"
            self._tripped_at[node_name] = time.time()

    def get_sla_stats(self, node_name: str) -> Tuple[float, float, float]:
        """计算当前节点在有效滑动窗口内的 (错误率%, 平均延迟ms, P95延迟ms)"""
        self._ensure_node(node_name)
        self._prune_expired(node_name)
        q = self._metrics.get(node_name)
        if not q:
            return 0.0, 0.0, 0.0
        total = len(q)
        failures = sum(1 for m in q if not m.success)
        durations = sorted(m.duration_ms for m in q)

        error_rate = (failures / total) * 100.0
        avg_latency = float(sum(durations) / total)
        p95_idx = int(total * 0.95)
        p95_latency = float(durations[min(p95_idx, total - 1)])
        return error_rate, avg_latency, p95_latency

    def should_alert(self, node_name: str) -> bool:
        """带有 5 分钟冷却的告警频控判断"""
        self._ensure_node(node_name)
        now = time.time()
        last_alert = self._last_alert_at.get(node_name, 0.0)
        if now - last_alert < 300.0:
            return False
        state = self.get_node_state(node_name)
        error_rate, _, _ = self.get_sla_stats(node_name)
        if state == "OPEN" or error_rate >= self.error_threshold_percent:
            return True
        return False

    def mark_alert_sent(self, node_name: str):
        """记录实际告警发送成功时间"""
        self._ensure_node(node_name)
        self._last_alert_at[node_name] = time.time()

    def reset_node(self, node_name: str, reset_timestamp: Optional[float] = None):
        """管理员重置节点统计与状态"""
        self._ensure_node(node_name)
        now = reset_timestamp or time.time()
        self._reset_at[node_name] = now
        self._metrics[node_name].clear()
        self._state[node_name] = "CLOSED"
        self._tripped_at.pop(node_name, None)
        self._last_alert_at.pop(node_name, None)
        self._consecutive_failures[node_name] = 0
        self._probe_in_flight[node_name] = False

    def hydrate_once(self, node_name: str, metrics: List[CallMetric], last_reset_ts: float = 0.0):
        """冷启动单次校准：仅在服务初始化时回填，防止轮询请求篡改运行态"""
        if self._hydrated.get(node_name, False):
            return
        self._hydrated[node_name] = True
        self._reset_at[node_name] = max(self._reset_at.get(node_name, 0.0), last_reset_ts)
        self.load_history_metrics(node_name, metrics)

    def load_history_metrics(self, node_name: str, metrics: List[CallMetric]):
        """从持久化数据库历史中回填指标队列并校准状态"""
        self._ensure_node(node_name)
        reset_time = self._reset_at.get(node_name, 0.0)
        valid_metrics = [m for m in metrics if m.timestamp >= reset_time]

        self._metrics[node_name].clear()
        for m in valid_metrics:
            self._metrics[node_name].append(m)

        if not valid_metrics:
            return

        error_rate, _, _ = self.get_sla_stats(node_name)
        sample_count = len(self._metrics[node_name])
        if sample_count >= self.min_calls and error_rate >= self.error_threshold_percent:
            self._state[node_name] = "OPEN"
            failed_times = [m.timestamp for m in valid_metrics if not m.success]
            self._tripped_at[node_name] = max(failed_times) if failed_times else time.time()
        else:
            self._state[node_name] = "CLOSED"
