"""src/rag_kb/core/resilience/alert.py —— 企业级多模型容灾预警与多端机器人 Webhook 推送器。

原生支持企业微信、钉钉、飞书等主流平台的 Markdown / 交互卡片协议格式。
自动附带实时可用率 (SLA %)、平均延迟、P95 延迟与故障归因。
内置 SSRF 安全出网校验与非阻塞异步推送。
"""

from datetime import datetime
import logging
from typing import Optional
import httpx
from rag_kb.core.config import settings
from rag_kb.core.network.egress import validate_outbound_url

logger = logging.getLogger("rag_kb.resilience.alert")

def build_alert_payload(
    target_url: str,
    node_name: str,
    err_category: str,
    detail: str,
    error_rate: float,
    avg_latency: float,
    p95_latency: float
) -> dict:
    """根据目标 Webhook URL 域名自适应适配平台 Schema"""
    sla_percent = max(0.0, 100.0 - error_rate)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    url_lower = target_url.lower()

    # 1. 企业微信机器人 (qyapi.weixin.qq.com) —— 必须使用 markdown.content 字段
    if "qyapi.weixin.qq.com" in url_lower:
        return {
            "msgtype": "markdown",
            "markdown": {
                "content": (
                    f"### <font color=\"warning\">⚠️ 大模型节点故障预警 (SLA 跳闸)</font>\n"
                    f"- **故障节点**：`{node_name}`\n"
                    f"- **错误归因**：`{err_category}`\n"
                    f"- **实时可用率**：SLA 成功率 `{sla_percent:.1f}%` (错误率: `{error_rate:.1f}%`)\n"
                    f"- **延迟指标**：平均耗时 `{avg_latency:.0f}ms` | P95 耗时 `{p95_latency:.0f}ms`\n"
                    f"- **故障详情**：{detail[:200]}\n"
                    f"- **容灾动作**：已自动将该节点置为 OPEN 熔断隔离，流量平滑转移至备用模型候选池\n"
                    f"- **告警时间**：`{now_str}`\n"
                )
            }
        }

    # 2. 飞书机器人 (open.feishu.cn / open.larksuite.com) —— 必须使用 interactive card 格式
    if "feishu.cn" in url_lower or "larksuite.com" in url_lower:
        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": "【企业级 RAG 生产高危预警】大模型服务异常"},
                    "template": "red"
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": (
                            f"**故障节点**：`{node_name}`\n"
                            f"**错误归因**：`{err_category}`\n"
                            f"**实时可用率**：SLA `{sla_percent:.1f}%` (错误率: `{error_rate:.1f}%`)\n"
                            f"**延迟指标**：平均耗时 `{avg_latency:.0f}ms` | P95 耗时 `{p95_latency:.0f}ms`\n"
                            f"**故障详情**：{detail[:200]}\n"
                            f"**容灾动作**：已自动将该节点置为 OPEN 熔断隔离，流量平滑转移至备用模型候选池\n"
                            f"**告警时间**：`{now_str}`"
                        )
                    }
                ]
            }
        }

    # 3. 钉钉机器人 (oapi.dingtalk.com) 与通用默认 Webhook
    return {
        "msgtype": "markdown",
        "markdown": {
            "title": "【企业级 RAG 生产高危预警】大模型服务异常",
            "text": (
                f"### ⚠️ 大模型节点故障预警 (SLA 跳闸)\n"
                f"- **故障节点**：`{node_name}`\n"
                f"- **错误归因**：`{err_category}`\n"
                f"- **实时可用率**：SLA 成功率 `{sla_percent:.1f}%` (错误率: `{error_rate:.1f}%`)\n"
                f"- **延迟指标**：平均耗时 `{avg_latency:.0f}ms` | P95 耗时 `{p95_latency:.0f}ms`\n"
                f"- **故障详情**：{detail[:200]}\n"
                f"- **容灾动作**：已自动将该节点置为 OPEN 熔断隔离，流量平滑转移至备用模型候选池\n"
                f"- **告警时间**：`{now_str}`\n"
            )
        }
    }

async def trigger_webhook_alert(
    node_name: str,
    err_category: str,
    detail: str,
    error_rate: float,
    avg_latency: float,
    p95_latency: float,
    webhook_url: Optional[str] = None
) -> bool:
    """
    向企业微信/钉钉/飞书推送带有 SLA 与延迟指标的高优先级告警卡片

    :return: 是否成功发送
    """
    raw_target_url = webhook_url or getattr(settings, "ALERT_WEBHOOK_URL", None)
    if not raw_target_url:
        return False

    try:
        # SSRF 安全校验拦截
        target_url = validate_outbound_url(raw_target_url)
    except Exception as ssrf_err:
        logger.error(f"[AlertWebhook] Webhook 目标 URL 触发 SSRF 安全拦截: {ssrf_err}")
        return False

    payload = build_alert_payload(
        target_url=target_url,
        node_name=node_name,
        err_category=err_category,
        detail=detail,
        error_rate=error_rate,
        avg_latency=avg_latency,
        p95_latency=p95_latency
    )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(target_url, json=payload)
            if resp.status_code >= 400:
                logger.warning(f"[AlertWebhook] 推送告警响应异常 HTTP {resp.status_code}: {resp.text}")
                return False
            return True
    except Exception as e:
        logger.warning(f"[AlertWebhook] 推送告警网络异常 (非阻塞): {e}")
        return False


def trigger_webhook_alert_sync(
    node_name: str,
    err_category: str,
    detail: str,
    error_rate: float,
    avg_latency: float,
    p95_latency: float,
    webhook_url: Optional[str] = None
) -> bool:
    """
    同步推送告警卡片（供 chat_sync / 工作线程安全调用，不依赖 asyncio 事件循环，返回值可用于频控冷却标记）
    """
    raw_target_url = webhook_url or getattr(settings, "ALERT_WEBHOOK_URL", None)
    if not raw_target_url:
        return False

    try:
        target_url = validate_outbound_url(raw_target_url)
    except Exception as ssrf_err:
        logger.error(f"[AlertWebhook] Webhook 目标 URL 触发 SSRF 安全拦截: {ssrf_err}")
        return False

    payload = build_alert_payload(
        target_url=target_url,
        node_name=node_name,
        err_category=err_category,
        detail=detail,
        error_rate=error_rate,
        avg_latency=avg_latency,
        p95_latency=p95_latency
    )

    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.post(target_url, json=payload)
            if resp.status_code >= 400:
                logger.warning(f"[AlertWebhook] 同步推送告警响应异常 HTTP {resp.status_code}: {resp.text}")
                return False
            return True
    except Exception as e:
        logger.warning(f"[AlertWebhook] 同步推送告警网络异常 (非阻塞): {e}")
        return False
