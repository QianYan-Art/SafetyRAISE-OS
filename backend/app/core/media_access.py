from datetime import UTC, datetime, timedelta

import jwt

from app.core.exceptions import AuthenticationError
from app.core.settings import AuthSettings

MEDIA_COOKIE = "sr_session_media"
MEDIA_COOKIE_PATH = "/api/v1/chat-sessions"
MEDIA_TTL_SECONDS = 900
MEDIA_AUDIENCE = "chat-session-media"


def issue_media_token(auth: AuthSettings, user_id: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": user_id,
            "aud": MEDIA_AUDIENCE,
            "iat": now,
            "exp": now + timedelta(seconds=MEDIA_TTL_SECONDS),
        },
        auth.jwt_secret,
        algorithm=auth.jwt_algorithm,
    )


def decode_media_token(auth: AuthSettings, token: str) -> str:
    try:
        payload = jwt.decode(
            token,
            auth.jwt_secret,
            algorithms=[auth.jwt_algorithm],
            audience=MEDIA_AUDIENCE,
            options={"require": ["sub", "aud", "exp"]},
        )
        if not payload["sub"]:
            raise AuthenticationError("媒体访问凭据无效，请重新打开预览。")
        return str(payload["sub"])
    except jwt.PyJWTError as exc:
        raise AuthenticationError("媒体访问已失效，请重新打开预览。") from exc
