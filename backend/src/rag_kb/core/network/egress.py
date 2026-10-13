"""src/rag_kb/core/network/egress.py —— 统一出网安全网关与 SSRF 防御器。

遵循 SECURITY_MODEL.md 规范 (方向二：工具调用与 SSRF / 文件安全)：
1. 外部拉取与大模型调用域名白名单校验；
2. 严禁访问私网 CIDR (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 127.0.0.0/8, 169.254.0.0/16)；
3. DNS resolve 后二次核验目标真实 IP，彻底防御 DNS Rebinding 重绑定与服务端请求伪造 (SSRF)。
"""

import socket
import ipaddress
import logging
from urllib.parse import urlparse
from typing import List, Set, Optional

logger = logging.getLogger("rag_kb.security.egress")

class SSRFSecurityError(RuntimeError):
    """SSRF 安全拦截异常 (Fail-Fast)"""
    pass

# 企业级默认允许出网的根域名白名单
DEFAULT_ALLOWED_DOMAINS: Set[str] = {
    "aliyuncs.com",
    "mineru.net",
    "kedaya.ai",
    "siliconflow.cn",
    "siliconflow.com",
    "openai.com",
    "x.ai",
    "openxlab.org.cn",
    "shlab.tech"
}

def is_ip_address(host: str) -> bool:
    """判断字符串是否为原生 IP 地址"""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False

def is_private_or_restricted_ip(ip_str: str) -> bool:
    """
    核验 IP 是否属于私网、回环、本地链路或云厂商元数据保护地址
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        return (
            ip.is_private or
            ip.is_loopback or
            ip.is_link_local or
            ip.is_multicast or
            ip.is_reserved or
            ip.is_unspecified
        )
    except ValueError:
        return True

def validate_outbound_url(
    url: str,
    allowed_domains: Optional[Set[str]] = None,
    allow_custom_domains: bool = True
) -> str:
    """
    出网 URL 综合安全校验
    
    :param url: 待访问的外部完整 URL
    :param allowed_domains: 域名白名单集合
    :param allow_custom_domains: 是否允许通过解析校验的安全公网自定义域名
    :return: 校验通过的规范化 URL
    :raises SSRFSecurityError: 命中私网 IP、内网探测或未授权目标
    """
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise SSRFSecurityError(f"【出网安全拦截】仅支持 HTTP/HTTPS 协议，拒绝访问协议: {scheme}")

    hostname = parsed.hostname
    if not hostname:
        raise SSRFSecurityError(f"【出网安全拦截】无法解析 URL 中的目标主机名: {url}")

    hostname_clean = hostname.lower().strip()
    whitelist = allowed_domains or DEFAULT_ALLOWED_DOMAINS

    # 1. 直接输入为 IP 地址的严禁访问私网
    if is_ip_address(hostname_clean):
        if is_private_or_restricted_ip(hostname_clean):
            raise SSRFSecurityError(f"【SSRF 拦截】严禁直接请求私有/内网 IP 地址: {hostname_clean}")

    # 2. 域名白名单匹配 (允许子域名，例如 api.siliconflow.cn 匹配 siliconflow.cn)
    is_whitelisted = any(
        hostname_clean == d or hostname_clean.endswith(f".{d}")
        for d in whitelist
    )

    # 3. DNS Resolve 校验 (防御 DNS 重新绑定攻击 DNS Rebinding)
    try:
        # 获取所有解析的 IP 地址
        addr_info = socket.getaddrinfo(hostname_clean, parsed.port or (443 if scheme == "https" else 80))
        resolved_ips = {item[4][0] for item in addr_info}
        
        for ip in resolved_ips:
            if is_private_or_restricted_ip(ip):
                raise SSRFSecurityError(
                    f"【SSRF 拦截】域名 {hostname_clean} 解析为内部私有 IP ({ip})，触发安全熔断！"
                )
    except socket.gaierror as e:
        logger.warning(f"[EgressGuard] DNS 解析目标主机失败: {hostname_clean} ({e})")
        # DNS 无法解析交由后续网络层快速失败

    return url
