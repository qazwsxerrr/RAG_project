"""src/rag_kb/core/security/__init__.py —— 企业级安全护栏与敏感词过滤模块导出。
"""

from rag_kb.core.security.aho_corasick import (
    AhoCorasickAutomaton,
    default_ac_automaton,
    DEFAULT_SENSITIVE_WORDS,
)
from rag_kb.core.security.prompt_guard import (
    validate_prompt_security,
    INJECTION_PATTERNS,
)
from rag_kb.core.security.stream_moderation import (
    StreamModerator,
    default_stream_moderator,
    moderate_stream,
)

__all__ = [
    "AhoCorasickAutomaton",
    "default_ac_automaton",
    "DEFAULT_SENSITIVE_WORDS",
    "validate_prompt_security",
    "INJECTION_PATTERNS",
    "StreamModerator",
    "default_stream_moderator",
    "moderate_stream",
]
