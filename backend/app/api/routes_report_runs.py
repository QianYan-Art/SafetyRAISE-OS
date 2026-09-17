from __future__ import annotations

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute

from app.api.deps import get_current_user
from app.api.error_handling import build_error_response
from app.report_harness.errors import HarnessError
from app.schemas.report_run import CreateRunRequest, ExecuteRunRequest
from app.services.auth_service import AuthenticatedUser
from app.services.report_run_service import ReportRunService


class ReportRunBodyLimit:
    """在解析 JSON 前限制实际字节数，包含未提供 Content-Length 的请求。"""

    def __init__(self, app, max_bytes=256 * 1024):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/v1/report-runs"):
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                return await build_error_response(
                    request=Request(scope), status_code=413, code="request_too_large",
                    message="请求体超过允许大小。", retryable=False,
                )(scope, receive, send)
            if not message.get("more_body", False):
                break
        consumed = False

        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return await self.app(scope, bounded_receive, send)


class HarnessRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except HarnessError as exc:
                return build_error_response(
                    request=request, status_code=exc.status_code, code=exc.code,
                    message=exc.code, retryable=False,
                )
            except RequestValidationError as exc:
                return build_error_response(
                    request=request, status_code=422, code="invalid_request",
                    message="报告运行请求参数不合法。", retryable=False,
                    details={"validation_errors": [
                        {"loc": list(error["loc"]), "type": error["type"]}
                        for error in exc.errors()
                    ]},
                )

        return handle


def get_report_run_service() -> ReportRunService:
    # 仅隔离测试应用覆盖此依赖；生产启用需后续 transport/预算验收。
    raise HarnessError("feature_unavailable", 503)


router = APIRouter(prefix="/api/v1/report-runs", tags=["report-runs"], route_class=HarnessRoute)


@router.post("", status_code=201)
def create_run(payload: CreateRunRequest,
               user: AuthenticatedUser = Depends(get_current_user),
               service: ReportRunService = Depends(get_report_run_service)):
    return service.create(user.id, payload)


@router.get("/{run_id}")
def get_run(run_id: UUID, user: AuthenticatedUser = Depends(get_current_user),
            service: ReportRunService = Depends(get_report_run_service)):
    return service.get(user.id, str(run_id))


@router.get("/{run_id}/candidate")
def get_candidate(run_id: UUID, user: AuthenticatedUser = Depends(get_current_user),
                  service: ReportRunService = Depends(get_report_run_service)):
    return service.candidate(user.id, str(run_id))


@router.get("/{run_id}/events")
def get_events(run_id: UUID, after_seq: int = Query(0, ge=0),
               limit: int = Query(100, ge=1, le=500),
               user: AuthenticatedUser = Depends(get_current_user),
               service: ReportRunService = Depends(get_report_run_service)):
    return service.store.events(user.id, str(run_id), after_seq, limit)


@router.post("/{run_id}/cancel")
def cancel_run(run_id: UUID, user: AuthenticatedUser = Depends(get_current_user),
               service: ReportRunService = Depends(get_report_run_service)):
    return service.cancel(user.id, str(run_id))


@router.post("/{run_id}/execute/stream")
async def execute_run(run_id: UUID, payload: ExecuteRunRequest,
                      user: AuthenticatedUser = Depends(get_current_user),
                      service: ReportRunService = Depends(get_report_run_service)):
    identifier = str(run_id)
    token = service.claim(user.id, identifier, payload.expected_version)

    async def stream():
        task = asyncio.create_task(service.execute_claimed(user.id, identifier, token))
        seq = 0
        try:
            while True:
                page = service.store.events(user.id, identifier, seq)
                for event in page["events"]:
                    seq = event["seq"]
                    yield "data: " + json.dumps(jsonable_encoder(event), ensure_ascii=False) + "\n\n"
                if task.done():
                    await task
                    # 最后一轮查询后完成的事务必须仍被发送。
                    page = service.store.events(user.id, identifier, seq)
                    for event in page["events"]:
                        yield "data: " + json.dumps(jsonable_encoder(event), ensure_ascii=False) + "\n\n"
                    break
                await asyncio.sleep(0.05)
        except Exception:
            # 仅重放控制器已持久化的事件，不伪造缺少序号的流式终态。
            page = service.store.events(user.id, identifier, seq)
            for event in page["events"]:
                yield "data: " + json.dumps(jsonable_encoder(event), ensure_ascii=False) + "\n\n"
        finally:
            try:
                if not task.done():
                    service.cancel_claimed(user.id, identifier, token)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
