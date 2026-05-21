from typing import Any

from pydantic import Field

from app.schemas.base import StrictModel


class ApiErrorPayload(StrictModel):
    code: str
    message: str
    retryable: bool = False
    trace_id: str
    details: dict[str, Any] | None = None


class ApiErrorResponse(StrictModel):
    error: ApiErrorPayload
    detail: str = Field(description="兼容旧前端的错误消息镜像字段。")
