from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.api.deps import get_chat_session_service
from app.schemas.chat_session import (
    ChatSessionLinkedArtifact,
    ChatSessionRecord,
    CreateChatSessionRequest,
    LinkedArtifactDetailResponse,
    UpdateChatSessionRequest,
)
from app.core.exceptions import SessionNotFoundError
from app.services.chat_session_service import ChatSessionService

router = APIRouter(prefix="/api/v1/chat-sessions", tags=["chat-sessions"])


@router.get("", response_model=list[ChatSessionRecord])
def list_chat_sessions(
    service: ChatSessionService = Depends(get_chat_session_service),
):
    return service.list_sessions()


@router.get("/{session_id}", response_model=ChatSessionRecord)
def get_chat_session(
    session_id: str,
    service: ChatSessionService = Depends(get_chat_session_service),
):
    return service.get_session(session_id)


@router.post("", response_model=ChatSessionRecord)
def create_chat_session(
    request: CreateChatSessionRequest,
    service: ChatSessionService = Depends(get_chat_session_service),
):
    return service.create_session(request)


@router.put("/{session_id}", response_model=ChatSessionRecord)
def update_chat_session(
    session_id: str,
    request: UpdateChatSessionRequest,
    service: ChatSessionService = Depends(get_chat_session_service),
):
    return service.update_session(session_id, request)


@router.delete("/{session_id}")
def delete_chat_session(
    session_id: str,
    service: ChatSessionService = Depends(get_chat_session_service),
):
    service.delete_session(session_id)
    return {"status": "success"}


@router.get("/{session_id}/linked-artifacts", response_model=list[ChatSessionLinkedArtifact])
def list_chat_session_linked_artifacts(
    session_id: str,
    service: ChatSessionService = Depends(get_chat_session_service),
):
    return service.list_linked_artifacts(session_id)


@router.get(
    "/{session_id}/linked-artifacts/{category}",
    response_model=LinkedArtifactDetailResponse,
)
def get_chat_session_linked_artifact_detail(
    session_id: str,
    category: str,
    service: ChatSessionService = Depends(get_chat_session_service),
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
    service: ChatSessionService = Depends(get_chat_session_service),
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
    )
