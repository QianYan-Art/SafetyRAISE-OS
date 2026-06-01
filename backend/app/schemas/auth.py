from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator

from app.schemas.base import StrictModel


class LoginRequest(StrictModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def _normalize_username(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("用户名不能为空。")
        return normalized


class RegisterRequest(StrictModel):
    username: str
    password: str
    display_name: Optional[str] = None

    @field_validator("username")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        normalized = value.strip()
        if not (4 <= len(normalized) <= 20):
            raise ValueError("用户名长度必须在 4 到 20 个字符之间。")
        return normalized

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) > 48:
            raise ValueError("显示名称不能超过 48 个字符。")
        return normalized or None


class UserSummaryResponse(StrictModel):
    id: str
    username: str
    display_name: Optional[str] = None
    role: Literal["admin", "user"]
    is_active: bool
    created_at: str
    updated_at: str


class AuthTokenResponse(StrictModel):
    access_token: str
    token_type: str = "bearer"
    user: UserSummaryResponse
