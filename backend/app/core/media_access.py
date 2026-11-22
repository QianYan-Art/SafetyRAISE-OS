from datetime import UTC, datetime, timedelta
import hashlib
import hmac

import jwt

from app.core.exceptions import AuthenticationError
from app.core.settings import AuthSettings

MEDIA_COOKIE = "sr_session_media"
MEDIA_COOKIE_PATH = "/api/v1/chat-sessions"
MEDIA_TTL_SECONDS = 900
MEDIA_AUDIENCE = "chat-session-media"


def _derive_media_signing_key(auth: AuthSettings) -> str:
    """从主密钥域分离派生独立媒体签名密钥，确保媒体令牌与访问令牌密钥隔离。"""
    return hmac.new(
        str(auth.jwt_secret or "").encode("utf-8"),
        b"safetyraise:media-access-token:v1",
        hashlib.sha256,
    ).hexdigest()


def issue_media_token(auth: AuthSettings, user_id: str, *, session_id: str | None = None) -> str:
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": user_id,
        "aud": MEDIA_AUDIENCE,
        "typ": "media",
        "iat": now,
        "exp": now + timedelta(seconds=MEDIA_TTL_SECONDS),
    }
    if session_id:
        payload["sid"] = session_id
    return jwt.encode(
        payload,
        _derive_media_signing_key(auth),
        algorithm=auth.jwt_algorithm,
    )


def decode_media_token(
    auth: AuthSettings,
    token: str,
    *,
    expected_session_id: str | None = None,
) -> str:
    try:
        payload = jwt.decode(
            token,
            _derive_media_signing_key(auth),
            algorithms=[auth.jwt_algorithm],
            audience=MEDIA_AUDIENCE,
            options={"require": ["sub", "aud", "exp"]},
        )
        if not payload["sub"]:
            raise AuthenticationError("媒体访问凭据无效，请重新打开预览。")
        token_session_id = str(payload.get("sid") or "").strip()
        if expected_session_id and token_session_id and token_session_id != expected_session_id:
            raise AuthenticationError("媒体访问凭据与当前会话不匹配，请重新打开预览。")
        return str(payload["sub"])
    except jwt.PyJWTError as exc:
        raise AuthenticationError("媒体访问已失效，请重新打开预览。") from exc
