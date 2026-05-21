from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.exceptions import WorkflowError
from app.core.request_context import get_trace_id
from app.schemas.errors import ApiErrorPayload, ApiErrorResponse

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(WorkflowError, workflow_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


def workflow_error_handler(request: Request, exc: WorkflowError) -> JSONResponse:
    error_response = build_error_response(
        request=request,
        code=exc.code,
        message=exc.public_message,
        retryable=exc.retryable,
        status_code=exc.status_code,
        details=exc.details,
    )
    log_level = logging.WARNING if exc.status_code < 500 else logging.ERROR
    logger.log(
        log_level,
        "受控异常 | route=%s | code=%s | retryable=%s | detail=%s",
        request.url.path,
        exc.code,
        exc.retryable,
        str(exc),
    )
    return error_response


def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = {
        "validation_errors": [
            {
                "loc": [str(item) for item in error.get("loc", ())],
                "msg": str(error.get("msg") or ""),
                "type": str(error.get("type") or ""),
            }
            for error in exc.errors()
        ]
    }
    logger.warning("请求校验失败 | route=%s | errors=%s", request.url.path, details["validation_errors"])
    return build_error_response(
        request=request,
        code="INVALID_REQUEST",
        message="请求参数不合法，请检查后重试。",
        retryable=False,
        status_code=400,
        details=details,
    )


def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else f"请求失败：{exc.status_code}"
    retryable = exc.status_code >= 500
    code = "INTERNAL_ERROR" if exc.status_code >= 500 else "INVALID_REQUEST"
    logger.warning(
        "HTTP 异常 | route=%s | status=%s | detail=%s",
        request.url.path,
        exc.status_code,
        message,
    )
    return build_error_response(
        request=request,
        code=code,
        message=message,
        retryable=retryable,
        status_code=exc.status_code,
        details={"source": "http_exception"},
    )


def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("未处理异常 | route=%s", request.url.path)
    return build_error_response(
        request=request,
        code="INTERNAL_ERROR",
        message="服务内部处理失败，请稍后重试；如持续失败，请联系维护者并提供错误追踪号。",
        retryable=False,
        status_code=500,
    )


def build_error_response(
    *,
    request: Request | None,
    code: str,
    message: str,
    retryable: bool,
    status_code: int,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    trace_id = get_request_trace_id(request)
    payload = ApiErrorResponse(
        error=ApiErrorPayload(
            code=code,
            message=message,
            retryable=retryable,
            trace_id=trace_id,
            details=details,
        ),
        detail=message,
    )
    response = JSONResponse(
        status_code=status_code,
        content=payload.model_dump(),
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


def build_sse_error_event(
    *,
    request: Request | None,
    code: str,
    message: str,
    retryable: bool,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    trace_id = get_request_trace_id(request)
    payload = ApiErrorPayload(
        code=code,
        message=message,
        retryable=retryable,
        trace_id=trace_id,
        details=details,
    )
    return {
        "event": "error",
        **payload.model_dump(),
    }


def get_request_trace_id(request: Request | None) -> str:
    if request is not None:
        trace_id = getattr(request.state, "trace_id", None)
        if isinstance(trace_id, str) and trace_id.strip():
            return trace_id
    return get_trace_id()
