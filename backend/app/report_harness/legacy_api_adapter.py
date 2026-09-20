from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import Request
from psycopg.rows import dict_row

from app.adapters.input.file_input_adapter import FileInputAdapter
from app.api import deps
from app.core.exceptions import AuthenticationError, InputValidationError, SessionNotFoundError, WorkflowError
from app.core.path_guard import resolve_api_path
from app.report_harness.authorization import AuthorizationRequest
from app.report_harness.config import ReportHarnessSettings
from app.report_harness.errors import HarnessError
from app.report_harness.evidence_store import EvidenceStore
from app.report_harness.store import RunStore
from app.schemas.chat_session import CreateChatSessionRequest, UpdateChatSessionRequest
from app.schemas.report_run import CreateRunRequest
from app.schemas.workflow import GenerateReportRequest, GenerateReportResponse
from app.services.auth_service import AuthenticatedUser
from app.services.chat_session_service import ChatSessionService
from app.services.report_run_service import ReportRunService


@dataclass(frozen=True, slots=True)
class LegacyHarnessContext:
    store: RunStore
    service: ReportRunService
    settings: object | None = None
    database_service: object | None = None
    chat_session_service: object | None = None


@dataclass(slots=True)
class LegacyHarnessRun:
    service: ReportRunService
    owner: str
    run_id: str
    expected_version: int
    trace_id: str
    completed_view: dict | None = None
    session_id: str | None = None
    source_type: str | None = None
    source_path: str | None = None
    session_created: bool = False
    input_generation: dict | None = None

    async def execute(self) -> dict:
        if self.completed_view is not None:
            return self.completed_view
        try:
            return await self.service.execute(self.owner, self.run_id, self.expected_version)
        except HarnessError as exc:
            raise _workflow_error(exc, run_id=self.run_id) from exc

    def execute_sync(self) -> dict:
        return asyncio.run(self.execute())

    def cancel(self) -> dict:
        try:
            return self.service.cancel(self.owner, self.run_id)
        except HarnessError as exc:
            raise _workflow_error(exc, run_id=self.run_id) from exc

    def events(self, after_seq: int = 0) -> dict:
        try:
            return self.service.store.events(self.owner, self.run_id, after_seq)
        except HarnessError as exc:
            raise _workflow_error(exc, run_id=self.run_id) from exc

    def guidance(self) -> dict:
        store = self.service.store
        if not hasattr(store, "connection"):
            return {}
        try:
            with store.connection() as conn:
                conn.row_factory = dict_row
                row = RunStore._read(conn, self.owner, self.run_id)
        except HarnessError as exc:
            raise _workflow_error(exc, run_id=self.run_id) from exc
        prepared = row["document"].get("prepared") or {}
        guidance = prepared.get("guidance")
        return dict(guidance) if isinstance(guidance, dict) else {}


def get_legacy_harness_context(request: Request) -> LegacyHarnessContext | None:
    """正式或显式开发入口只装配 harness；离线模式不触碰数据库依赖。"""
    settings = deps.get_settings()
    config = ReportHarnessSettings.model_validate(
        getattr(settings, "report_harness", ReportHarnessSettings())
    )
    runtime = getattr(request.app.state, "report_harness_runtime", None)
    development_runtime = getattr(
        request.app.state, "report_harness_development_runtime", None,
    )
    blocked_reason = getattr(request.app.state, "report_harness_blocked_reason", None)
    production_ready = bool(
        runtime is not None
        and getattr(runtime, "production_outbound_enabled", False) is True
    )
    development_ready = bool(
        development_runtime is not None
        and getattr(development_runtime, "development_outbound_enabled", False) is True
        and getattr(development_runtime, "force_engineering_exports", False) is True
        and getattr(development_runtime, "business_workflow", None) is not None
    )
    if production_ready:
        selected_runtime = runtime
    elif development_ready:
        # 这是 business_server 显式登记的隔离开发运行时；它仍由下游
        # engineering-only 质量门阻止正式成功和正式导出。
        selected_runtime = development_runtime
    elif config.online_enabled:
        # 正式运行时不可用时，历史旧产物的导出仍可依靠持久归属校验；
        # 生成请求必须继续报 503，不能借此回退 ReportService.generate。
        if "/exports/" in str(request.url.path):
            return None
        if blocked_reason in {"resource_pressure", "resource_probe_failed"}:
            raise WorkflowError(
                "正式报告运行时因资源检查暂不可用，未回退旧报告服务。",
                code=blocked_reason,
                status_code=503,
                retryable=True,
            )
        raise WorkflowError(
            "正式报告运行时尚未初始化。",
            code="outbound_transport_unavailable",
            status_code=503,
            retryable=False,
        )
    else:
        # 任意未登记的 app.state 对象都不能改变离线旧接口语义。
        return None

    database_factory = request.app.dependency_overrides.get(
        deps.get_database_service, deps.get_database_service,
    )
    with _legacy_database_access():
        database = database_factory()
        store = RunStore(database.connection)
    return LegacyHarnessContext(
        store=store,
        service=ReportRunService(store, selected_runtime),
        settings=settings,
        database_service=database,
    )


def get_legacy_harness_export_record(
    context: LegacyHarnessContext | None,
    trace_id: str,
    owner: str,
) -> dict | None:
    """按旧 trace_id 识别 harness run，并通过 owner-scoped store 读取。"""
    run_id = _legacy_harness_run_id(trace_id)
    if context is None or run_id is None:
        return None
    try:
        return context.store.get(owner, run_id)
    except HarnessError as exc:
        raise _workflow_error(exc, run_id=run_id) from exc


def prepare_legacy_run(
    context: LegacyHarnessContext,
    request: GenerateReportRequest,
    current_user: AuthenticatedUser | None,
    http_request: Request,
) -> LegacyHarnessRun:
    if current_user is None:
        raise AuthenticationError(
            "正式在线报告接口需要先登录。",
            code="AUTHENTICATION_REQUIRED",
        )
    owner = current_user.id
    request_id = _request_id(http_request, owner)
    source_type = _legacy_source_type(request)
    with _legacy_database_access():
        session_id, session_created = _ensure_legacy_session(
            context,
            current_user,
            request.session_id,
            request_id,
            request.accident_data,
            source_type=source_type,
            source_path=None,
        )
    accident_data, source_type, source_path, input_generation = _resolve_legacy_input(
        context,
        request,
        http_request,
    )
    if session_created:
        with _legacy_database_access():
            _persist_legacy_session_snapshot(
                context,
                current_user,
                session_id,
                accident_data,
                source_type=source_type,
                source_path=source_path,
            )
    try:
        evidence_revision = EvidenceStore(context.store).get(
            owner, session_id,
        )["revision"]
        created = context.service.create(owner, CreateRunRequest(
            request_id=request_id,
            session_id=session_id,
            accident_data=accident_data,
            evidence_revision=evidence_revision,
        ))
        run_id = str(created["run_id"])
        state = created.get("state")
        trace_id = f"report-{run_id}"
        if state == "published":
            return LegacyHarnessRun(
                service=context.service,
                owner=owner,
                run_id=run_id,
                expected_version=int(created["state_version"]),
                trace_id=trace_id,
                completed_view=created,
                session_id=session_id,
                source_type=source_type,
                source_path=source_path,
                session_created=session_created,
                input_generation=input_generation,
            )
        if state != "queued":
            raise WorkflowError(
                "该旧接口请求对应的报告运行不可再次执行。",
                code="legacy_run_not_retriable",
                status_code=409,
                details={
                    "run_id": run_id,
                    "state": state,
                    "terminal_reason": created.get("terminal_reason"),
                },
            )

        preview = context.service.authorization_preview(owner, run_id)
        if not preview.get("available"):
            raise HarnessError("authorization_profile_unavailable")
        authorized = context.service.authorize(owner, run_id, AuthorizationRequest(
            snapshot_digest=preview["snapshot_digest"],
            endpoint_profile_digest=preview["endpoint_profile_digest"],
            approved_knowledge_manifest_digest=preview["approved_knowledge_manifest_digest"],
            confirmed=True,
        ))
        return LegacyHarnessRun(
            service=context.service,
            owner=owner,
            run_id=run_id,
            expected_version=int(authorized["state_version"]),
            trace_id=trace_id,
            session_id=session_id,
            source_type=source_type,
            source_path=source_path,
            session_created=session_created,
            input_generation=input_generation,
        )
    except HarnessError as exc:
        raise _workflow_error(exc) from exc


def build_legacy_response(run: LegacyHarnessRun, view: dict) -> GenerateReportResponse:
    if (
        view.get("state") != "published"
        or not isinstance(view.get("report"), dict)
        or view.get("formal_export_eligible") is not True
    ):
        raise WorkflowError(
            "报告已由 harness 处理，但尚未达到正式发布条件。",
            code="legacy_formal_success_unavailable",
            status_code=409,
            details={
                "run_id": run.run_id,
                "state": view.get("state"),
                "quality_gate": view.get("quality_gate"),
                "formal_export_eligible": view.get("formal_export_eligible"),
                "terminal_reason": view.get("terminal_reason"),
            },
        )
    return GenerateReportResponse(
        trace_id=run.trace_id,
        status="success",
        output_dir="",
        guidance=run.guidance(),
        report=view["report"],
        initial_knowledge_snippets=[],
        knowledge_snippets=[],
        input_generation=run.input_generation,
        retrieval_meta={
            "report_run_id": run.run_id,
            "session_id": run.session_id,
            "session_created": run.session_created,
            "legacy_source_type": run.source_type,
            "legacy_source_path": run.source_path,
        },
        agentic_retrieval_rounds=[],
    )


def _resolve_legacy_input(
    context: LegacyHarnessContext,
    request: GenerateReportRequest,
    http_request: Request,
) -> tuple[dict, str, str | None, dict | None]:
    if request.input_path:
        settings = context.settings or deps.get_settings()
        resolved = resolve_api_path(
            settings,
            request.input_path,
            field_name="input_path",
            allowed_roots=[
                settings.backend_data_dir_path,
                settings.input_generation_workspace_dir_path,
                settings.output_dir_path,
            ],
        )
        if not resolved.is_file():
            raise InputValidationError("input_path 不存在或不是文件。")
        return FileInputAdapter(str(resolved)).load(), "input_path", str(resolved), None
    if request.video_path:
        settings = context.settings or deps.get_settings()
        resolved = resolve_api_path(
            settings,
            request.video_path,
            field_name="video_path",
            allowed_roots=[
                settings.backend_data_dir_path,
                settings.input_generation_workspace_dir_path,
                settings.resolve_path("backend/data/runtime/uploads"),
            ],
        )
        if not resolved.is_file():
            raise InputValidationError("video_path 不存在或不是文件。")
        input_service_factory = http_request.app.dependency_overrides.get(
            deps.get_input_generation_service,
            deps.get_input_generation_service,
        )
        input_service = input_service_factory()
        try:
            artifact = input_service.generate(
                video_path=str(resolved),
                persist_generated_input=request.persist_generated_input,
            )
        finally:
            _close_input_generation_service(input_service)
        generated_input = getattr(artifact, "generated_input", None)
        if not isinstance(generated_input, dict) or not generated_input:
            raise WorkflowError(
                "原视觉输入生成未返回有效事故快照，报告未启动。",
                code="legacy_video_input_generation_invalid",
                status_code=502,
                details={"source": "video_path"},
            )
        model_dump = getattr(artifact, "model_dump", None)
        input_generation = model_dump() if callable(model_dump) else None
        if not isinstance(input_generation, dict):
            raise WorkflowError(
                "原视觉输入生成结果无法保持旧响应契约，报告未启动。",
                code="legacy_video_input_generation_invalid",
                status_code=502,
                details={"source": "video_path"},
            )
        return generated_input, "video_path", str(resolved), input_generation
    if request.accident_data is None:
        raise WorkflowError(
            "正式在线报告接口需要提供 accident_data、input_path 或 video_path。",
            code="legacy_accident_data_required",
            status_code=422,
            details={"supported_contract": "accident_data 或 input_path(JSON)"},
        )
    return request.accident_data, "accident_data", None, None


@contextmanager
def _legacy_database_access():
    """仅包装数据库准备阶段，不改写归属与输入校验错误。"""
    try:
        yield
    except (WorkflowError, HarnessError):
        raise
    except Exception as exc:
        raise WorkflowError(
            "报告数据库暂不可用，未回退旧报告服务。",
            code="legacy_database_unavailable",
            status_code=503,
        ) from exc


def _ensure_legacy_session(
    context: LegacyHarnessContext,
    current_user: AuthenticatedUser,
    session_id: str | None,
    request_id: UUID,
    accident_data: dict | None,
    *,
    source_type: str,
    source_path: str | None,
) -> tuple[str, bool]:
    session_service = _get_legacy_chat_session_service(context, current_user)
    if session_id:
        # ChatSessionService.get_session 在加载记录后执行 owner 校验；必须先做这一步，
        # 才能读取路径或调用原视觉输入服务。
        record = session_service.get_session(
            session_id,
            include_linked_files=False,
            include_linked_artifacts=False,
        )
        return str(record.id), False
    generated_id = f"legacy-report-{request_id.hex}"
    try:
        existing = session_service.get_session(
            generated_id, include_linked_files=False, include_linked_artifacts=False,
        )
        return existing.id, False
    except SessionNotFoundError:
        pass
    source_name = Path(source_path).name if source_path else "legacy-report-api"
    created = session_service.create_session(CreateChatSessionRequest(
        id=generated_id,
        title="交通事故分析报告",
        source_type=f"legacy_report_{source_type}",
        source_name=source_name,
        draft_json=json.dumps(accident_data or {}, ensure_ascii=False),
        draft_meta={
            "legacy_report_api": True,
            "legacy_source_type": source_type,
            "legacy_source_name": source_name,
        },
    ))
    return created.id, True


def _get_legacy_chat_session_service(
    context: LegacyHarnessContext,
    current_user: AuthenticatedUser,
) -> object:
    if context.chat_session_service is not None:
        return context.chat_session_service
    settings = context.settings or deps.get_settings()
    if context.database_service is not None:
        # ChatSessionService 构造函数会立即初始化默认数据库；在线请求必须复用
        # 已通过依赖注入取得的同一连接工厂，避免旁路配置或测试数据库。
        session_service = ChatSessionService.__new__(ChatSessionService)
        session_service.settings = settings
        session_service.current_user = current_user
        session_service.database_service = context.database_service
        session_service._history_retriever = None
        session_service._history_retriever_ready = False
        return session_service
    return ChatSessionService(settings=settings, current_user=current_user)


def _persist_legacy_session_snapshot(
    context: LegacyHarnessContext,
    current_user: AuthenticatedUser,
    session_id: str,
    accident_data: dict,
    *,
    source_type: str,
    source_path: str | None,
) -> None:
    session_service = _get_legacy_chat_session_service(context, current_user)
    record = session_service.get_session(
        session_id,
        include_linked_files=False,
        include_linked_artifacts=False,
    )
    draft_meta = dict(getattr(record, "draft_meta", None) or {})
    draft_meta.update({
        "legacy_report_api": True,
        "legacy_source_type": source_type,
        "legacy_source_name": Path(source_path).name if source_path else "legacy-report-api",
    })
    session_service.update_session(
        session_id,
        UpdateChatSessionRequest(
            draft_json=json.dumps(accident_data, ensure_ascii=False),
            draft_meta=draft_meta,
            source_type=f"legacy_report_{source_type}",
            source_name=draft_meta["legacy_source_name"],
        ),
    )


def _legacy_source_type(request: GenerateReportRequest) -> str:
    if request.input_path:
        return "input_path"
    if request.video_path:
        return "video_path"
    return "accident_data"


def _close_input_generation_service(service: object) -> None:
    close = getattr(service, "close", None)
    if callable(close):
        close()


def _request_id(request: Request, owner: str) -> UUID:
    raw = (
        request.headers.get("Idempotency-Key", "").strip()
        or request.headers.get("X-Request-Id", "").strip()
    )
    if not raw:
        return uuid4()
    try:
        return UUID(raw)
    except ValueError:
        return uuid5(NAMESPACE_URL, f"legacy-report:{owner}:{raw}")


def _legacy_harness_run_id(trace_id: str) -> str | None:
    value = str(trace_id or "").strip()
    prefix = "report-"
    if not value.startswith(prefix):
        return None
    try:
        return str(UUID(value[len(prefix):]))
    except ValueError:
        return None


def _workflow_error(exc: HarnessError, *, run_id: str | None = None) -> WorkflowError:
    details = dict(exc.details or {})
    if run_id is not None:
        details["run_id"] = run_id
    return WorkflowError(
        exc.code,
        code=exc.code,
        status_code=exc.status_code,
        retryable=False,
        details=details or None,
        public_message=_harness_message(exc.code),
    )


def _harness_message(code: str) -> str:
    return {
        "not_found": "报告运行或会话不存在，或当前账号无权访问。",
        "authorization_profile_unavailable": "正式报告授权目录不可用，未启动模型请求。",
        "authorization_stale": "正式报告授权已失效，未启动模型请求。",
        "release_not_approved": "当前正式发布绑定未获批准，未启动模型请求。",
        "outbound_transport_unavailable": "正式报告外发运行时不可用，未回退旧报告服务。",
        "resource_pressure": "当前资源不足，报告未启动或已暂停。",
        "storage_capacity_insufficient": "当前存储资源不足，报告未启动。",
        "memory_capacity_insufficient": "当前内存资源不足，报告未启动。",
        "authorization_required": "正式报告授权未完成，未启动模型请求。",
        "authorization_state_conflict": "报告运行当前状态不允许授权。",
        "evidence_revision_conflict": "会话证据已变化，请重新发起报告请求。",
        "active_run_conflict": "当前会话已有活动报告运行，请勿重复发起。",
        "idempotency_conflict": "同一幂等键已绑定到不同的报告请求。",
    }.get(code, "正式报告运行失败，未回退旧报告服务。")
