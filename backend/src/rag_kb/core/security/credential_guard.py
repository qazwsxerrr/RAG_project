"""src/rag_kb/core/security/credential_guard.py —— 核心系统凭据与云私钥外泄硬熔断守卫。

严格监控大模型输出流中的高危敏感凭证：
1. 阿里云 AccessKey ID (LTAI...) 与 AccessKey Secret；
2. AWS 访问密钥 (AKIA...) 与 Secret Access Key；
3. 非对称私钥 (-----BEGIN RSA/EC/OPENSSH PRIVATE KEY-----)；
4. 带明文密码的数据库物理连接串 (PostgreSQL / MySQL / Redis / MongoDB)；
5. 系统核心 JWT Secret、API Secret 敏感配置代码行。

一旦在输出流中嗅探到上述任一凭证，立即执行 P0 物理级硬熔断，终止推流并断开连接！
"""

import re
import logging
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__)

# 凭据熔断专属告警提示
CREDENTIAL_FUSE_MESSAGE = "\n\n【系统安全拦截】检测到输出内容包含企业核心系统密钥或连接凭据，已强制终止响应！"

# ==================== P0 凭据正则特征模式 ====================

CREDENTIAL_PATTERNS: List[Tuple[str, re.Pattern]] = [
    # 1. 阿里云 AccessKey ID
    ("阿里云 AccessKey ID", re.compile(r"(?<![a-zA-Z0-9])(LTAI[a-zA-Z0-9]{16,24})(?![a-zA-Z0-9])")),
    # 2. 阿里云 AccessKey Secret
    ("阿里云 AccessKey Secret", re.compile(r"(?i)(?:access_?key_?secret|oss_?access_?key_?secret)[\s:=]+['\"]?([a-zA-Z0-9]{28,32})['\"]?")),
    # 3. AWS Access Key ID
    ("AWS Access Key ID", re.compile(r"(?<![a-zA-Z0-9])(AKIA[0-9A-Z]{16})(?![a-zA-Z0-9])")),
    # 4. AWS Secret Access Key
    ("AWS Secret Key", re.compile(r"(?i)(?:aws_?secret_?access_?key)[\s:=]+['\"]?([0-9a-zA-Z/+]{40})['\"]?")),
    # 5. 非对称密码体系私钥头部
    ("非对称私钥头部", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----")),
    # 6. 带明文密码的数据库连接串 (要求密码长度 >= 4 且非通用 mock 占位符)
    ("生产数据库连接串", re.compile(r"(?i)\b(?:postgresql|postgres|mysql|mongodb|redis):\/\/[a-zA-Z0-9_\-\.]+:(?!(?:password|xxx|123456|<password>|\*\*\*)[@\s])[^@\s]{4,}@[a-zA-Z0-9_\-\.]+(?::\d+)?\/[a-zA-Z0-9_\-\.]*")),
    # 7. 硬编码高危 API/JWT 签名密钥 (长度 >= 24)
    ("高危系统签名密钥", re.compile(r"(?i)\b(?:api[_-]?secret[_-]?key|jwt[_-]?secret[_-]?key|app[_-]?secret)[\s:=]+['\"]([a-zA-Z0-9_\-]{24,})['\"]")),
]

def detect_credential_leak(text: str) -> Optional[str]:
    """
    嗅探文本中是否包含核心系统凭据与私钥。
    :param text: 待检测文本片段
    :return: 命中凭据名称或 None
    """
    if not text:
        return None

    for cred_name, pattern in CREDENTIAL_PATTERNS:
        match = pattern.search(text)
        if match:
            matched_content = match.group(0)
            # 安全脱敏打印日志，防止日志中造成二次泄露
            masked_log = matched_content[:4] + "***" + matched_content[-4:] if len(matched_content) > 8 else "***"
            logger.critical(f"【P0 凭据熔断】触发高危凭据泄漏防护！类型: {cred_name}, 签名片段: {masked_log}")
            return cred_name

    return None
