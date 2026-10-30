"""src/rag_kb/core/security/__init__.py —— 企业级安全护栏、敏感词过滤与隐私脱敏模块导出。
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
    FUSE_BREAKER_MESSAGE,
)
from rag_kb.core.security.pii_masker import (
    mask_all_pii,
    StreamingPIIMasker,
)
from rag_kb.core.security.credential_guard import (
    detect_credential_leak,
    CREDENTIAL_FUSE_MESSAGE,
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
    "FUSE_BREAKER_MESSAGE",
    "mask_all_pii",
    "StreamingPIIMasker",
    "detect_credential_leak",
    "CREDENTIAL_FUSE_MESSAGE",
]
