from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import deps, routes_chat_sessions
from app.core.exceptions import AuthenticationError, SessionNotFoundError, WorkflowError
from app.core.media_access import MEDIA_AUDIENCE, MEDIA_COOKIE, decode_media_token, issue_media_token
from app.core.security import create_access_token
from app.core.settings import AuthSettings


@pytest.fixture
def media_client(tmp_path, monkeypatch):
    settings_dependency = deps.get_settings
    settings = SimpleNamespace(auth=AuthSettings(jwt_secret="test-media-only-" + "x" * 40), runtime_profile="local")
    owner = SimpleNamespace(id="owner", username="owner")
    live_users = {"owner": owner, "other": SimpleNamespace(id="other", username="other")}
    image = tmp_path / "sample.png"
    image.write_bytes(b"sample-image-bytes")

    class Auth:
        def get_user_by_id(self, user_id):
            if user_id not in live_users:
                raise AuthenticationError("账号不可用")
            return live_users[user_id]

    class Sessions:
        def __init__(self, settings, current_user):
            self.settings = settings
            self.current_user = current_user

        def get_session(self, session_id, **_kwargs):
            if session_id != "owned" or self.current_user.id != "owner":
                raise SessionNotFoundError("会话不存在")
            return SimpleNamespace(id=session_id)

        def resolve_linked_artifact_asset(self, session_id, category, asset_id):
            self.get_session(session_id)
            if category != "images_and_keyframes" or asset_id != "image":
                raise SessionNotFoundError("媒体不存在")
            return SimpleNamespace(path=str(image), mime_type="image/png", file_name=image.name)

    monkeypatch.setattr(routes_chat_sessions, "ChatSessionService", Sessions)
    monkeypatch.setattr(deps, "ChatSessionService", Sessions)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(routes_chat_sessions.router)
    app.dependency_overrides[settings_dependency] = lambda: settings
    app.dependency_overrides[deps.get_auth_service] = lambda: Auth()

    @app.exception_handler(WorkflowError)
    async def error(_request: Request, exc: WorkflowError):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    with TestClient(app, base_url="https://testserver") as client:
        yield client, settings, live_users


def _headers(settings, user_id="owner"):
    return {"Authorization": "Bearer " + create_access_token(
        auth_settings=settings.auth, user_id=user_id, username=user_id, role="user",
    )}


def test_media_cookie_is_scoped_and_preserves_range(media_client):
    client, settings, _ = media_client
    url = "/api/v1/chat-sessions/owned/linked-artifacts/images_and_keyframes/assets/image"
    assert client.get(url).status_code == 401
    assert client.post("/api/v1/chat-sessions/owned/media-access", headers=_headers(settings, "other")).status_code == 404
    granted = client.post("/api/v1/chat-sessions/owned/media-access", headers=_headers(settings))
    assert granted.status_code == 200
    assert "HttpOnly" in granted.headers["set-cookie"]
    assert "SameSite=strict" in granted.headers["set-cookie"]
    response = client.get(url, headers={"Range": "bytes=1-3"})
    assert response.status_code == 206
    assert response.content == b"amp"
    assert response.headers["content-range"] == "bytes 1-3/18"
    assert response.headers["cache-control"] == "private, no-store"
    assert client.get(url.replace("/owned/", "/another/")).status_code == 404
    assert client.get("/api/v1/chat-sessions/owned").status_code == 401
    assert client.get(url, headers={"Authorization": "Bearer " + client.cookies.get(MEDIA_COOKIE)}).status_code == 401
    assert client.delete("/api/v1/chat-sessions/media-access").status_code == 200
    assert client.get(url).status_code == 401


def test_deleted_account_cannot_use_media_cookie(media_client):
    client, settings, live_users = media_client
    assert client.post("/api/v1/chat-sessions/owned/media-access", headers=_headers(settings)).status_code == 200
    del live_users["owner"]
    assert client.get("/api/v1/chat-sessions/owned/linked-artifacts/images_and_keyframes/assets/image").status_code == 401


def test_media_token_rejects_expiry_and_wrong_audience():
    auth = AuthSettings(jwt_secret="test-media-only-" + "x" * 40)
    token = issue_media_token(auth, "owner")
    assert decode_media_token(auth, token) == "owner"
    expired = jwt.encode(
        {"sub": "owner", "aud": MEDIA_AUDIENCE,
         "exp": datetime.now(UTC) - timedelta(seconds=1)},
        auth.jwt_secret, algorithm=auth.jwt_algorithm,
    )
    with pytest.raises(AuthenticationError):
        decode_media_token(auth, expired)
    with pytest.raises(AuthenticationError):
        decode_media_token(auth, create_access_token(
            auth_settings=auth, user_id="owner", username="owner", role="user",
        ))
