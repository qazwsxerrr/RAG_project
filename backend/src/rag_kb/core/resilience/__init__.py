"""src/rag_kb/core/resilience —— 全局弹性容错与重试包"""

from rag_kb.core.resilience.retry import retry_on_transient, is_transient_error

__all__ = ["retry_on_transient", "is_transient_error"]
