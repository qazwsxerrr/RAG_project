"""src/rag_kb/core/resilience —— 全局弹性容错、熔断状态机与多模型容灾预警包"""

from rag_kb.core.resilience.retry import retry_on_transient, is_transient_error
from rag_kb.core.resilience.classifier import classify_error
from rag_kb.core.resilience.circuit_breaker import ModelHealthTracker, CallMetric
from rag_kb.core.resilience.alert import trigger_webhook_alert

__all__ = [
    "retry_on_transient",
    "is_transient_error",
    "classify_error",
    "ModelHealthTracker",
    "CallMetric",
    "trigger_webhook_alert",
]
