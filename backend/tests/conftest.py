import time
from pathlib import Path
from typing import Dict, List, Optional
import jwt
import pytest
import pytest_asyncio
import rag_kb.api.deps as deps_module
from rag_kb.core.config import settings
from rag_kb.core.redis_client import close_redis_clients
from rag_kb.db.session import engine

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

KEYS_DIR = Path(__file__).resolve().parent.parent.parent / "keys"
TEST_PRIVATE_KEY_PATH = KEYS_DIR / "rsa_private.pem"
TEST_PUBLIC_KEY_PATH = KEYS_DIR / "rsa_public.pem"
_EPHEMERAL_RSA_PAIR: Optional[tuple[str, str]] = None
_LAST_ISSUED_TOKEN_MS = 0


def _get_test_rsa_keypair_pems() -> tuple[str, str]:
    """优先读取本地 keys/ 密钥对；若干净检出未包含私钥文件，则在进程内生成临时测试 RSA-2048 密钥对"""
    global _EPHEMERAL_RSA_PAIR
    if TEST_PRIVATE_KEY_PATH.exists() and TEST_PUBLIC_KEY_PATH.exists():
        return (
            TEST_PRIVATE_KEY_PATH.read_text(encoding="utf-8"),
            TEST_PUBLIC_KEY_PATH.read_text(encoding="utf-8"),
        )
    if _EPHEMERAL_RSA_PAIR is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("utf-8")
        pub_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("utf-8")
        _EPHEMERAL_RSA_PAIR = (priv_pem, pub_pem)
    return _EPHEMERAL_RSA_PAIR


def make_rs256_test_token(
    user_id: str = "user_dev_01",
    tenant_id: str = "default_tenant",
    username: str = "测试研发工程师",
    dept_path: str = "/总公司/研发中心/技术部",
    dept_paths: Optional[List[str]] = None,
    scope_by_action: Optional[Dict[str, str]] = None,
    data_scope: Optional[str] = None,
    position_code: str = "STAFF",
    clearance_level: int = 2,
    authz_ver: int = 1,
    policy_epoch: int = 1,
    roles: Optional[list] = None,
    idp_roles: Optional[list] = None,
    perms: Optional[list] = None,
    is_to_c: bool = False,
    cv: int = 2,
    exp_delta: int = 3600,
    issuer: str = "rag-admin",
    audience: str = "rag-engine",
    key_pem: Optional[str] = None,
    custom_claims: Optional[dict] = None,
) -> str:
    """共享 RS256 测试令牌签发工具（默认签发 cv=2 多维权限契约，支持显式指定 cv=1 做负向测试）"""
    if roles is None:
        roles = ["developer"]
    if idp_roles is None:
        idp_roles = list(roles)
    if perms is None:
        perms = ["kb:retrieve", "doc:upload", "doc:delete", "doc:review"]
    if dept_paths is None:
        dept_paths = [dept_path] if dept_path and dept_path != "/" else []

    if scope_by_action is None:
        mapped_scope = data_scope or "DEPT_AND_SUB"
        scope_by_action = {
            "READ": mapped_scope,
            "UPLOAD": mapped_scope,
            "REVIEW": mapped_scope,
            "DELETE": mapped_scope,
            "MANAGE": mapped_scope,
        }

    private_pem = key_pem or _get_test_rsa_keypair_pems()[0]
    global _LAST_ISSUED_TOKEN_MS
    now_ms = max(int(time.time() * 1000), _LAST_ISSUED_TOKEN_MS + 1)
    _LAST_ISSUED_TOKEN_MS = now_ms
    now = now_ms // 1000
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "username": username,
        "dept_path": dept_path,
        "dept_paths": dept_paths,
        "scope_by_action": scope_by_action,
        "position_code": position_code,
        "clearance_level": clearance_level,
        "authz_ver": authz_ver,
        "policy_epoch": policy_epoch,
        "roles": roles,
        "idp_roles": idp_roles,
        "perms": perms,
        "is_to_c": is_to_c,
        "cv": cv,
        "typ": "access",
        "iat": now,
        "iat_ms": now_ms,
        "exp": now + exp_delta,
        "iss": issuer,
        "aud": audience,
    }
    if custom_claims:
        payload.update(custom_claims)
    return jwt.encode(payload, private_pem, algorithm="RS256")


@pytest.fixture
def local_rsa_jwt_env(monkeypatch):
    """局部将 JWT 验签指向本地测试公钥（无 keys/ 时自动使用进程内测试公钥）并自动清理全局缓存"""
    monkeypatch.setattr(settings, "JWT_JWKS_URL", None)
    if TEST_PUBLIC_KEY_PATH.exists():
        monkeypatch.setattr(settings, "JWT_PUBLIC_KEY", None)
        monkeypatch.setattr(settings, "JWT_PUBLIC_KEY_PATH", str(TEST_PUBLIC_KEY_PATH))
    else:
        monkeypatch.setattr(settings, "JWT_PUBLIC_KEY", _get_test_rsa_keypair_pems()[1])
        monkeypatch.setattr(settings, "JWT_PUBLIC_KEY_PATH", None)
    monkeypatch.setattr(deps_module, "_PUBLIC_KEY_CACHE", None)
    monkeypatch.setattr(deps_module, "_JWKS_CLIENT", None)
    yield
    deps_module._PUBLIC_KEY_CACHE = None
    deps_module._JWKS_CLIENT = None


@pytest_asyncio.fixture(autouse=True)
async def cleanup_db_engine():
    yield
    await close_redis_clients()
    await engine.dispose()
