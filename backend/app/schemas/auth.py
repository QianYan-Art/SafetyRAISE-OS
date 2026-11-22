from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator

from app.schemas.base import StrictModel

MAX_PASSWORD_LENGTH = 128
MAX_PASSWORD_BYTES = 256


def _validate_password_length(value: str, *, require_min: bool = False) -> str:
    normalized = value.strip()
    if require_min and len(normalized) < 8:
        raise ValueError("密码长度不能少于 8 位。")
    if len(normalized) > MAX_PASSWORD_LENGTH or len(normalized.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(f"密码长度不能超过 {MAX_PASSWORD_LENGTH} 个字符。")
    return value


class LoginRequest(StrictModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def _normalize_username(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 64:
            raise ValueError("用户名不能为空且不能超过 64 个字符。")
        return normalized

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        return _validate_password_length(value, require_min=False)


class RegisterRequest(StrictModel):
    username: str
    password: str
    display_name: Optional[str] = None
    invite_code: Optional[str] = None

    @field_validator("username")
    @classmethod
    def _validate_username(cls, value: str) -> str:
        normalized = value.strip()
        if not (4 <= len(normalized) <= 20):
            raise ValueError("用户名长度必须在 4 到 20 个字符之间。")
        return normalized

    @field_validator("password")
    @classmethod
    def _validate_password(cls, value: str) -> str:
        return _validate_password_length(value, require_min=True)

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) > 48:
            raise ValueError("显示名称不能超过 48 个字符。")
        return normalized or None


class ChangePasswordRequest(StrictModel):
    current_password: str
    new_password: str

    @field_validator("current_password")
    @classmethod
    def _validate_current_password(cls, value: str) -> str:
        return _validate_password_length(value, require_min=False)

    @field_validator("new_password")
    @classmethod
    def _validate_new_password(cls, value: str) -> str:
        return _validate_password_length(value, require_min=True)


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
