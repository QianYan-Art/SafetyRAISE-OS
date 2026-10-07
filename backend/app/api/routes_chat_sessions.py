from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from app.api.deps import (
    get_auth_service,
    get_authed_chat_session_service,
    get_optional_current_user,
    get_settings,
)
from app.core.media_access import (
    MEDIA_COOKIE, MEDIA_COOKIE_PATH, MEDIA_TTL_SECONDS, decode_media_token, issue_media_token,
)
from app.core.settings import Settings
from app.schemas.chat_session import (
    ChatSessionLinkedArtifact,
    ChatSessionRecord,
    CreateChatSessionRequest,
    LinkedArtifactDetailResponse,
    UpdateChatSessionRequest,
)
from app.core.exceptions import AuthenticationError, SessionNotFoundError
from app.services.auth_service import AuthenticatedUser, AuthService
from app.services.chat_session_service import ChatSessionService

router = APIRouter(prefix="/api/v1/chat-sessions", tags=["chat-sessions"])


def get_media_chat_session_service(
    session_id: str,
    request: Request,
    user: AuthenticatedUser | None = Depends(get_optional_current_user),
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> ChatSessionService:
    if user is None:
        token = request.cookies.get(MEDIA_COOKIE)
        if not token:
            raise AuthenticationError("请先登录后再打开媒体预览。")
        user = auth_service.get_user_by_id(decode_media_token(settings.auth, token))
    return ChatSessionService(settings=settings, current_user=user)


@router.delete("/media-access")
def clear_media_access(response: Response):
    response.delete_cookie(MEDIA_COOKIE, path=MEDIA_COOKIE_PATH)
    response.headers["Cache-Control"] = "no-store"
    return {"status": "success"}


@router.post("/{session_id}/media-access")
def grant_media_access(
    session_id: str,
    request: Request,
    response: Response,
    service: ChatSessionService = Depends(get_authed_chat_session_service),
):
    service.get_session(session_id, include_linked_files=False, include_linked_artifacts=False)
    response.set_cookie(
        MEDIA_COOKIE,
        issue_media_token(service.settings.auth, service.current_user.id),
        max_age=MEDIA_TTL_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https" or service.settings.runtime_profile == "server",
        samesite="strict",
        path=MEDIA_COOKIE_PATH,
    )
    response.headers["Cache-Control"] = "no-store"
    return {"status": "success", "expires_in": MEDIA_TTL_SECONDS}


@router.get("", response_model=list[ChatSessionRecord])
def list_chat_sessions(
    service: ChatSessionService = Depends(get_authed_chat_session_service),
):
    return service.list_sessions(recover_unlinked_outputs=False)


@router.get("/{session_id}", response_model=ChatSessionRecord)
def get_chat_session(
    session_id: str,
    service: ChatSessionService = Depends(get_authed_chat_session_service),
):
    return service.get_session(session_id)


@router.post("", response_model=ChatSessionRecord)
def create_chat_session(
    request: CreateChatSessionRequest,
    service: ChatSessionService = Depends(get_authed_chat_session_service),
):
    return service.create_session(request)


@router.put("/{session_id}", response_model=ChatSessionRecord)
def update_chat_session(
    session_id: str,
    request: UpdateChatSessionRequest,
    service: ChatSessionService = Depends(get_authed_chat_session_service),
):
    return service.update_session(session_id, request)


@router.delete("/{session_id}")
def delete_chat_session(
    session_id: str,
    service: ChatSessionService = Depends(get_authed_chat_session_service),
):
    service.delete_session(session_id)
    return {"status": "success"}


@router.get("/{session_id}/linked-artifacts", response_model=list[ChatSessionLinkedArtifact])
def list_chat_session_linked_artifacts(
    session_id: str,
    service: ChatSessionService = Depends(get_authed_chat_session_service),
):
    return service.list_linked_artifacts(session_id)


@router.get(
    "/{session_id}/linked-artifacts/{category}",
    response_model=LinkedArtifactDetailResponse,
)
def get_chat_session_linked_artifact_detail(
    session_id: str,
    category: str,
    service: ChatSessionService = Depends(get_authed_chat_session_service),
):
    try:
        return service.get_linked_artifact_detail(session_id, category)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{session_id}/linked-artifacts/{category}/assets/{asset_id}")
def get_chat_session_linked_artifact_asset(
    session_id: str,
    category: str,
    asset_id: str,
    service: ChatSessionService = Depends(get_media_chat_session_service),
):
    try:
        asset = service.resolve_linked_artifact_asset(session_id, category, asset_id)
    except SessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    asset_path = Path(asset.path).resolve()
    return FileResponse(
        path=str(asset_path),
        media_type=asset.mime_type or "application/octet-stream",
        filename=asset.file_name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store", "Vary": "Cookie, Authorization"},
    )
