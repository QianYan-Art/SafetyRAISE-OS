from typing import Any, Optional

from pydantic import BaseModel, Field

from app.schemas.base import StrictModel


class ChatMessageRecord(BaseModel):
    id: str
    role: str
    kind: str
    content: str
    meta: Optional[dict[str, Any]] = None


class ChatSessionLinkedFile(BaseModel):
    label: str
    path: str
    category: str
    path_type: str = "file"
    exists: bool = True


class ChatSessionLinkedArtifact(BaseModel):
    label: str
    category: str
    kind: str = "collection"
    item_count: int = 0
    summary: str = ""


class ChatSessionRecord(BaseModel):
    id: str
    title: str = "新交通事故"
    created_at: int
    updated_at: int
    sort_order: Optional[int] = None
    source_type: Optional[str] = None
    source_name: Optional[str] = None
    messages: list[ChatMessageRecord] = Field(default_factory=list)
    draft_json: str = ""
    draft_meta: Optional[dict[str, Any]] = None
    report_result: Optional[dict[str, Any]] = None
    linked_files: list[ChatSessionLinkedFile] = Field(default_factory=list)
    linked_artifacts: list[ChatSessionLinkedArtifact] = Field(default_factory=list)
    session_state: str = "draft"


class CreateChatSessionRequest(StrictModel):
    id: Optional[str] = None
    title: str = "新交通事故"
    created_at: Optional[int] = None
    updated_at: Optional[int] = None
    sort_order: Optional[int] = None
    source_type: Optional[str] = None
    source_name: Optional[str] = None
    messages: list[ChatMessageRecord] = Field(default_factory=list)
    draft_json: str = ""
    draft_meta: Optional[dict[str, Any]] = None
    report_result: Optional[dict[str, Any]] = None


class UpdateChatSessionRequest(StrictModel):
    title: Optional[str] = None
    updated_at: Optional[int] = None
    sort_order: Optional[int] = None
    source_type: Optional[str] = None
    source_name: Optional[str] = None
    messages: Optional[list[ChatMessageRecord]] = None
    draft_json: Optional[str] = None
    draft_meta: Optional[dict[str, Any]] = None
    report_result: Optional[dict[str, Any]] = None


class LinkedArtifactAsset(StrictModel):
    asset_id: str
    kind: str
    media_type: str
    file_name: str
    path: str
    mime_type: Optional[str] = None
    category_id: Optional[str] = None
    category_label: Optional[str] = None
    source_name: Optional[str] = None
    reason: Optional[str] = None
    sequence: Optional[int] = None
    timestamp_seconds: Optional[float] = None
    annotation_label: Optional[str] = None


class LinkedArtifactDetailResponse(StrictModel):
    category: str
    label: str
    kind: str
    summary: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    content: list[dict[str, Any]] = Field(default_factory=list)
    assets: list[LinkedArtifactAsset] = Field(default_factory=list)
