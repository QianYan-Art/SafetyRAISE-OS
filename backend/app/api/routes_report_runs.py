from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from fastapi.routing import APIRoute

from app.api import deps
from app.api.deps import get_current_user, get_database_service
from app.api.error_handling import build_error_response
from app.report_harness.errors import HarnessError
from app.report_harness.authorization import AuthorizationCatalog, AuthorizationRequest
from app.report_harness.config import ReportHarnessSettings
from app.report_harness.contracts import canonical_digest
from app.report_harness.execution import (
    ReportExecutionDependencies, outbound_ready, production_dependencies,
)
from app.report_harness.store import RunStore
from app.report_harness.release_registry import FileReleaseRegistry, verified_code_digest
from app.report_harness.prompts import load_role_prompts
from app.report_harness.role_loop import tool_schemas
from app.schemas.report_run import CreateRunRequest, ExecuteRunRequest, ResumeRunRequest
from app.services.auth_service import AuthenticatedUser
from app.services.report_run_service import ReportRunService


logger = logging.getLogger(__name__)
EVENT_POLL_SECONDS = 0.5
SSE_HEARTBEAT_SECONDS = 15.0


class ReportRunBodyLimit:
    """在解析 JSON 前限制实际字节数，包含未提供 Content-Length 的请求。"""

    def __init__(self, app, max_bytes=256 * 1024):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        protected = scope["type"] == "http" and (
            scope["path"].startswith("/api/v1/report-runs")
            or (scope["path"].startswith("/api/v1/chat-sessions/")
                and scope["path"].endswith("/report-evidence"))
        )
        if not protected:
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
                    message=exc.code, retryable=False, details=exc.details,
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


def get_report_run_service(database=Depends(get_database_service),
                           request: Request = None) -> ReportRunService:
    config = getattr(deps.get_settings(), "report_harness", ReportHarnessSettings())
    config = ReportHarnessSettings.model_validate(config)
    if not config.enabled:
        raise HarnessError("feature_unavailable", 503)
    if config.online_enabled:
        runtime = (getattr(request.app.state, "report_harness_runtime", None)
                   if request is not None else None)
        if not outbound_ready(runtime):
            blocked = (getattr(request.app.state, "report_harness_blocked_reason", None)
                       if request is not None else None)
            read_or_cancel = request is not None and (
                request.method == "GET" or (
                    request.method == "POST" and request.url.path.endswith("/cancel")
                )
            )
            if blocked in {"resource_pressure", "resource_probe_failed"} and read_or_cancel:
                def unavailable():
                    raise HarnessError("outbound_transport_unavailable", 503)

                runtime = ReportExecutionDependencies(
                    roles_factory=unavailable, execution_profile="outbound",
                    endpoint_profile_digest=canonical_digest("unavailable"),
                    policy_digest=canonical_digest("unavailable"),
                    knowledge_manifest_digest=canonical_digest("unavailable"),
                    force_engineering_exports=True,
                )
                store = RunStore(database.connection)
                store.check_schema()
                return ReportRunService(store, runtime)
            raise HarnessError("outbound_transport_unavailable", 503)
        store = RunStore(database.connection)
        store.check_schema()
        return ReportRunService(store, runtime)
    production_dependencies(config.model_dump(mode="json"))
    store = RunStore(database.connection)
    store.check_schema()
    registry = FileReleaseRegistry()
    registry.validate()
    catalog = None
    if config.endpoints:
        catalog = AuthorizationCatalog(
            config.endpoints, config.knowledge_collections,
            frozenset(config.approved_knowledge_manifests),
        )

    def unavailable_roles():
        raise HarnessError("outbound_transport_unavailable", 503)

    dependencies = ReportExecutionDependencies(
        roles_factory=unavailable_roles, execution_profile="outbound",
        endpoint_profile_digest=(catalog.endpoint_digest if catalog else
                                 canonical_digest({"endpoints": "unconfigured"})),
        policy_digest=canonical_digest({"version": config.policy_version,
                                        "budget": config.budget.model_dump(mode="json"),
                                        "prompts": load_role_prompts(),
                                        "tools": tool_schemas(),
                                        "context_policy": "bounded-source-reading-v1"}),
        knowledge_manifest_digest=(catalog.knowledge_digest if catalog else
                                   canonical_digest([])),
        authorization_catalog=catalog,
        budget_policy=config.budget,
        release_registry=registry,
        code_digest=verified_code_digest(),
    )
    return ReportRunService(store, dependencies)


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


@router.get("/{run_id}/authorization-preview")
def authorization_preview(run_id: UUID, user: AuthenticatedUser = Depends(get_current_user),
                          service: ReportRunService = Depends(get_report_run_service)):
    return service.authorization_preview(user.id, str(run_id))


@router.post("/{run_id}/authorize")
def authorize_run(run_id: UUID, payload: AuthorizationRequest,
                  user: AuthenticatedUser = Depends(get_current_user),
                  service: ReportRunService = Depends(get_report_run_service)):
    return service.authorize(user.id, str(run_id), payload)


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
    return _claimed_stream(identifier, user, service, token)


@router.post("/{run_id}/resume/stream")
async def resume_run(run_id: UUID, payload: ResumeRunRequest,
                     user: AuthenticatedUser = Depends(get_current_user),
                     service: ReportRunService = Depends(get_report_run_service)):
    identifier = str(run_id)
    token = service.resume_claim(
        user.id, identifier, payload.expected_version,
        retry_unknown_requests=payload.retry_unknown_requests,
    )
    return _claimed_stream(identifier, user, service, token)


def _claimed_stream(identifier: str, user, service: ReportRunService, token: int):
    async def stream():
        task = asyncio.create_task(service.execute_claimed(user.id, identifier, token))
        seq = 0
        last_sent = asyncio.get_running_loop().time()
        try:
            while True:
                page = service.store.events(user.id, identifier, seq)
                for event in page["events"]:
                    seq = event["seq"]
                    last_sent = asyncio.get_running_loop().time()
                    yield "data: " + json.dumps(jsonable_encoder(event), ensure_ascii=False) + "\n\n"
                if task.done():
                    await task
                    # 最后一轮查询后完成的事务必须仍被发送。
                    page = service.store.events(user.id, identifier, seq)
                    for event in page["events"]:
                        yield "data: " + json.dumps(jsonable_encoder(event), ensure_ascii=False) + "\n\n"
                    break
                if asyncio.get_running_loop().time() - last_sent >= SSE_HEARTBEAT_SECONDS:
                    yield ": keep-alive\n\n"
                    last_sent = asyncio.get_running_loop().time()
                await asyncio.sleep(EVENT_POLL_SECONDS)
        except Exception:
            # 仅重放控制器已持久化的事件，不伪造缺少序号的流式终态。
            try:
                page = service.store.events(user.id, identifier, seq)
            except HarnessError as exc:
                if exc.code != "not_found":
                    raise
                return
            for event in page["events"]:
                yield "data: " + json.dumps(jsonable_encoder(event), ensure_ascii=False) + "\n\n"
        finally:
            # 断连的取消域不能再次打断持久取消及 transport 关闭。
            with anyio.CancelScope(shield=True):
                try:
                    if not task.done():
                        logger.info("报告事件流提前关闭，取消执行。", extra={"run_id": identifier})
                        service.cancel_claimed(user.id, identifier, token)
                finally:
                    if not task.done() and not task.cancelling():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
