"""src/rag_kb/core/resilience/circuit_breaker.py —— 模型健康追踪器、SLA滑动窗口与熔断状态机。

维护状态：
- CLOSED: 正常健康，放行全部请求
- OPEN: 熔断隔离，阻断请求直接漂移备用模型
- HALF_OPEN: 冷却期过后，放行单次探测流量试探自愈
"""

import time
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

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
        cooldown_seconds: int = 300,
        error_threshold_percent: float = 5.0
    ):
        self.window_size = window_size
        self.cooldown_seconds = cooldown_seconds
        self.error_threshold_percent = error_threshold_percent

        # 节点指标双向队列: node_name -> deque[CallMetric]
        self._metrics: Dict[str, deque] = {}
        # 节点熔断状态: node_name -> "CLOSED" | "OPEN" | "HALF_OPEN"
        self._state: Dict[str, str] = {}
        # 熔断触发时间: node_name -> float
        self._tripped_at: Dict[str, float] = {}
        # 上次告警时间: node_name -> float (防止告警风暴)
        self._last_alert_at: Dict[str, float] = {}
        # 管理员手动重置时间戳: node_name -> float
        self._reset_at: Dict[str, float] = {}

    def _ensure_node(self, node_name: str):
        if node_name not in self._metrics:
            self._metrics[node_name] = deque(maxlen=self.window_size)
            self._state[node_name] = "CLOSED"

    def get_node_state(self, node_name: str) -> str:
        """获取节点当前熔断状态"""
        self._ensure_node(node_name)
        state = self._state.get(node_name, "CLOSED")
        if state == "OPEN":
            tripped_time = self._tripped_at.get(node_name, 0.0)
            if time.time() - tripped_time >= self.cooldown_seconds:
                self._state[node_name] = "HALF_OPEN"
                return "HALF_OPEN"
        return state

    def is_node_available(self, node_name: str) -> bool:
        """检查节点可用性并驱动 HALF_OPEN 状态流转"""
        state = self.get_node_state(node_name)
        if state in ("CLOSED", "HALF_OPEN"):
            return True
        return False

    def record_success(self, node_name: str, duration_ms: int = 0, ttft_ms: Optional[int] = None):
        """记录成功并自愈 HALF_OPEN 节点"""
        self._ensure_node(node_name)
        self._metrics[node_name].append(CallMetric(time.time(), True, duration_ms, ttft_ms, None))
        if self._state.get(node_name) == "HALF_OPEN":
            self._state[node_name] = "CLOSED"
            self._tripped_at.pop(node_name, None)

    def record_failure(self, node_name: str, duration_ms: int = 0, err_category: str = "ERR_UNKNOWN"):
        """记录失败并评估是否跳闸熔断"""
        self._ensure_node(node_name)
        self._metrics[node_name].append(CallMetric(time.time(), False, duration_ms, None, err_category))
        
        # 若处于 HALF_OPEN 试探失败，立即重新进入 OPEN 熔断
        if self._state.get(node_name) == "HALF_OPEN":
            self._state[node_name] = "OPEN"
            self._tripped_at[node_name] = time.time()
            return

        # 计算当前滑动窗口内的错误率
        error_rate, avg_latency, p95_latency = self.get_sla_stats(node_name)
        if error_rate >= self.error_threshold_percent:
            self._state[node_name] = "OPEN"
            self._tripped_at[node_name] = time.time()

    def get_sla_stats(self, node_name: str) -> Tuple[float, float, float]:
        """计算当前节点的 (错误率%, 平均延迟ms, P95延迟ms)"""
        self._ensure_node(node_name)
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
        error_rate, _, _ = self.get_sla_stats(node_name)
        if error_rate >= self.error_threshold_percent:
            self._last_alert_at[node_name] = now
            return True
        return False

    def reset_node(self, node_name: str):
        """重置节点统计与状态"""
        self._metrics[node_name] = deque(maxlen=self.window_size)
        self._state[node_name] = "CLOSED"
        self._tripped_at.pop(node_name, None)
        self._last_alert_at.pop(node_name, None)
        self._reset_at[node_name] = time.time()

    def load_history_metrics(self, node_name: str, metrics: list):
        """从持久化数据库历史中回填指标队列并校准状态 (冷启动对齐)"""
        self._ensure_node(node_name)
        reset_time = self._reset_at.get(node_name, 0.0)
        valid_metrics = [m for m in metrics if m.timestamp >= reset_time]

        self._metrics[node_name].clear()
        for m in valid_metrics:
            self._metrics[node_name].append(m)

        if not valid_metrics:
            return

        error_rate, _, _ = self.get_sla_stats(node_name)
        if error_rate >= self.error_threshold_percent:
            self._state[node_name] = "OPEN"
            failed_times = [m.timestamp for m in valid_metrics if not m.success]
            self._tripped_at[node_name] = max(failed_times) if failed_times else time.time()
        else:
            self._state[node_name] = "CLOSED"
