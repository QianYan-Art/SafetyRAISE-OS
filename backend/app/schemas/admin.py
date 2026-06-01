from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator

from app.schemas.base import StrictModel


class AdminCreateUserRequest(StrictModel):
    username: str
    password: str
    display_name: Optional[str] = None
    role: Literal["admin", "user"] = "user"
    is_active: bool = True

    @field_validator("username")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        normalized = value.strip()
        if not (4 <= len(normalized) <= 20):
            raise ValueError("用户名长度必须在 4 到 20 个字符之间。")
        return normalized


class AdminUpdateUserRequest(StrictModel):
    display_name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[Literal["admin", "user"]] = None
    is_active: Optional[bool] = None


class AdminUserResponse(StrictModel):
    id: str
    username: str
    display_name: Optional[str] = None
    role: Literal["admin", "user"]
    is_active: bool
    created_at: str
    updated_at: str


class AdminSpaceRecord(StrictModel):
    session_id: str
    owner_user_id: Optional[str] = None
    owner_username: Optional[str] = None
    title: str
    created_at: int
    updated_at: int
    session_state: str
    source_type: Optional[str] = None
    source_name: Optional[str] = None
    message_count: int = 0
    linked_artifact_count: int = 0
    redacted: bool = True


class AdminUpdateSpaceRequest(StrictModel):
    title: Optional[str] = None
    owner_user_id: Optional[str] = None
    sort_order: Optional[int] = None


class AdminCleanupSpacesResponse(StrictModel):
    status: str = "success"
    deleted_count: int = Field(default=0, ge=0)
