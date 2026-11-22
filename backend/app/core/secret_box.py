from __future__ import annotations

import hashlib
import hmac
import os

from app.core.settings import AuthSettings

PGCRYPTO_PREFIX = "pgcrypto:v1:"
ENCRYPTION_KEY_ENV = "USER_CONFIG_ENCRYPTION_KEY"


def derive_user_config_encryption_key(auth_settings: AuthSettings) -> str:
    """获取用户模型 API Key 静态加密密钥。

    优先读取独立环境变量 `USER_CONFIG_ENCRYPTION_KEY`；未单独配置时，
    由 `jwt_secret` 经域分离 HMAC-SHA256 派生独立密钥，不直接复用 JWT 签名原值。
    """
    explicit = os.getenv(ENCRYPTION_KEY_ENV, "").strip()
    if explicit:
        return explicit
    return hmac.new(
        str(auth_settings.jwt_secret or "").encode("utf-8"),
        b"safetyraise:user-api-key-encryption:v1",
        hashlib.sha256,
    ).hexdigest()


def pgcrypto_encrypt_sql() -> str:
    """返回使用 PostgreSQL pgcrypto AES-256 加密 api_key 的 SQL 片段。"""
    return (
        "case when %s::text is null or %s::text = '' then null "
        f"else '{PGCRYPTO_PREFIX}' || encode(pgp_sym_encrypt(%s::text, %s::text, 'cipher-algo=aes256'), 'base64') end"
    )


def pgcrypto_encrypt_params(plain_api_key: str | None, encryption_key: str) -> tuple[str | None, str | None, str | None, str]:
    normalized = (plain_api_key or "").strip() or None
    return (normalized, normalized, normalized, encryption_key)


def pgcrypto_decrypt_sql(column: str = "api_key") -> str:
    """返回兼容历史明文与 pgcrypto:v1 密文的解密 SQL 表达式。"""
    prefix_len = len(PGCRYPTO_PREFIX) + 1
    return (
        f"case when {column} is null then null "
        f"when {column} like '{PGCRYPTO_PREFIX}%%' "
        f"then pgp_sym_decrypt(decode(substring({column} from {prefix_len}), 'base64'), %s::text) "
        f"else {column} end as {column}"
    )
