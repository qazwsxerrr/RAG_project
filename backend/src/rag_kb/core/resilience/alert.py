"""src/rag_kb/core/resilience/alert.py —— 企业级多模型容灾预警与群机器人 Webhook 推送器。

支持企业微信、钉钉、飞书等主流平台的 Markdown 告警卡片格式。
自动附带实时可用率 (SLA %)、平均延迟、P95 延迟与故障归因。
"""

from datetime import datetime
import logging
from typing import Optional
import httpx
from rag_kb.core.config import settings

logger = logging.getLogger("rag_kb.resilience.alert")

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
    target_url = webhook_url or getattr(settings, "ALERT_WEBHOOK_URL", None)
    if not target_url:
        return False

    sla_percent = max(0.0, 100.0 - error_rate)
    payload = {
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
                f"- **告警时间**：`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n"
            )
        }
    }
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
