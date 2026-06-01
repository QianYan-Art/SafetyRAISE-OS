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


def hash_password(password: str) -> str:
    normalized = password.strip()
    if len(normalized) < 8:
        raise InputValidationError("密码长度不能少于 8 位。")
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
        password.strip().encode("utf-8"),
        salt,
        iterations,
    )
    return hmac.compare_digest(actual, expected)


def create_access_token(
    *,
    auth_settings: AuthSettings,
    user_id: str,
    username: str,
    role: str,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "username": username,
        "role": role,
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
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError("登录状态已失效，请重新登录。") from exc
    if not isinstance(payload, dict):
        raise AuthenticationError("登录状态无效，请重新登录。")
    return payload
