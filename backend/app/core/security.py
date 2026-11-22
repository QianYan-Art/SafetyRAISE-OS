from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.core.exceptions import AuthenticationError, InputValidationError
from app.core.settings import AuthSettings

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 200_000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
MAX_PASSWORD_BYTES = 256
ACCESS_TOKEN_AUDIENCE = "safetyraise-access"
# 预计算固定虚拟哈希，供未知用户名登录路径执行同成本校验，消除时序枚举差异。
_DUMMY_PASSWORD_HASH = (
    f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}$"
    f"{base64.b64encode(b'safetyraise-dummy').decode('ascii')}$"
    f"{base64.b64encode(hashlib.pbkdf2_hmac('sha256', b'dummy-password-value', b'safetyraise-dummy', PASSWORD_ITERATIONS)).decode('ascii')}"
)


def validate_password_bounds(password: str, *, require_min_length: bool = True) -> str:
    normalized = password.strip()
    if require_min_length and len(normalized) < MIN_PASSWORD_LENGTH:
        raise InputValidationError(f"密码长度不能少于 {MIN_PASSWORD_LENGTH} 位。")
    if len(normalized) > MAX_PASSWORD_LENGTH or len(normalized.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise InputValidationError(f"密码长度不能超过 {MAX_PASSWORD_LENGTH} 个字符。")
    return normalized


def hash_password(password: str) -> str:
    normalized = validate_password_bounds(password, require_min_length=True)
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        normalized.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS,
    )
    return (
        f"{PASSWORD_SCHEME}${PASSWORD_ITERATIONS}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(digest).decode('ascii')}"
    )


def verify_password(password: str, password_hash: str) -> bool:
    normalized = password.strip()
    if len(normalized) > MAX_PASSWORD_LENGTH or len(normalized.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return False
    try:
        scheme, iterations_raw, salt_raw, digest_raw = password_hash.split("$", 3)
    except ValueError:
        return False
    if scheme != PASSWORD_SCHEME:
        return False
    try:
        iterations = int(iterations_raw)
        salt = base64.b64decode(salt_raw.encode("ascii"))
        expected = base64.b64decode(digest_raw.encode("ascii"))
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        normalized.encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def verify_password_constant_time(password: str, password_hash: str | None) -> bool:
    """无论目标账号是否存在，均执行同成本 PBKDF2 校验，防止响应时间泄露有效用户名。"""
    if not password_hash:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        return False
    return verify_password(password, password_hash)


def create_access_token(
    *,
    auth_settings: AuthSettings,
    user_id: str,
    username: str,
    role: str,
    token_version: int = 1,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
        "aud": ACCESS_TOKEN_AUDIENCE,
        "typ": "access",
        "ver": int(token_version),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=auth_settings.access_token_ttl_minutes)).timestamp()),
    }
    return jwt.encode(
        payload,
        auth_settings.jwt_secret,
        algorithm=auth_settings.jwt_algorithm,
    )


def decode_access_token(token: str, auth_settings: AuthSettings) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            auth_settings.jwt_secret,
            algorithms=[auth_settings.jwt_algorithm],
            audience=ACCESS_TOKEN_AUDIENCE,
            options={"require": ["sub", "iat", "exp", "aud"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("登录状态已失效，请重新登录。") from exc
    if not isinstance(payload, dict) or payload.get("typ") not in {None, "access"}:
        raise AuthenticationError("登录状态无效，请重新登录。")
    return payload
