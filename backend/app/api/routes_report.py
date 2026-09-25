import asyncio
import json
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app.api import deps
from app.api.deps import (
    get_current_user,
    get_optional_current_user,
    get_report_export_service,
    get_report_service,
    get_user_capability_config_service,
)
from app.api.error_handling import build_sse_error_event
from app.core.exceptions import (
    AuthenticationError,
    PermissionDeniedError,
    RequestCancelledError,
    WorkflowError,
)
from app.core.path_guard import resolve_api_path
from app.report_harness.errors import HarnessError
from app.report_harness.legacy_api_adapter import (
    LegacyHarnessContext,
    build_legacy_response,
    get_legacy_harness_export_record,
    get_legacy_harness_context,
    prepare_legacy_run,
)
from app.report_harness.legacy_ownership import (
    LegacyOwnershipError,
    assert_legacy_session_access,
    is_legacy_export_authorized,
    persist_legacy_report_ownership,
)
from app.report_harness.exports import render_run_export
from app.report_harness.release_registry import export_eligibility
from app.schemas.workflow import GenerateReportRequest, GenerateReportResponse
from app.services.auth_service import AuthenticatedUser
from app.services.report_export_service import PdfCoverDateMode, PdfCoverOptions, ReportExportFormat, ReportExportService
from app.services.report_service import ReportService
from app.services.user_capability_config_service import UserCapabilityConfigService

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])
REPORT_STREAM_QUEUE_POLL_SECONDS = 0.2
REPORT_STREAM_HEARTBEAT_SECONDS = 15.0


@router.post("/generate", response_model=GenerateReportResponse)
def generate_report(
    http_request: Request,
    request: GenerateReportRequest,
    service: ReportService = Depends(get_report_service),
    current_user: AuthenticatedUser | None = Depends(get_optional_current_user),
    capability_config_service: UserCapabilityConfigService = Depends(get_user_capability_config_service),
    harness_context: LegacyHarnessContext | None = Depends(get_legacy_harness_context),
):
    _authorize_report_request(request, current_user)
    if harness_context is not None:
        run = prepare_legacy_run(harness_context, request, current_user, http_request)
        return build_legacy_response(run, run.execute_sync())
    try:
        assert_legacy_session_access(http_request, request.session_id, current_user)
    except LegacyOwnershipError as exc:
        raise WorkflowError(
            str(exc),
            code=exc.code,
            status_code=exc.status_code,
            public_message=str(exc),
        ) from exc
    artifact = _run_report_generation(
        service=service,
        request=request,
        current_user=current_user,
        capability_config_service=capability_config_service,
    )
    try:
        ownership = persist_legacy_report_ownership(
            http_request,
            request,
            artifact,
            current_user,
            settings=getattr(service, "settings", None) or deps.get_settings(),
        )
    except LegacyOwnershipError as exc:
        raise WorkflowError(
            str(exc),
            code=exc.code,
            status_code=exc.status_code,
            public_message=str(exc),
        ) from exc
    return _build_generate_report_response(artifact, session_id=ownership.session_id)


@router.post("/generate/stream")
def generate_report_stream(
    http_request: Request,
    request: GenerateReportRequest,
    service: ReportService = Depends(get_report_service),
    current_user: AuthenticatedUser | None = Depends(get_optional_current_user),
    capability_config_service: UserCapabilityConfigService = Depends(get_user_capability_config_service),
    harness_context: LegacyHarnessContext | None = Depends(get_legacy_harness_context),
):
    _authorize_report_request(request, current_user)
    if harness_context is not None:
        run = prepare_legacy_run(harness_context, request, current_user, http_request)
        return _generate_harness_report_stream(http_request, run)
    try:
        assert_legacy_session_access(http_request, request.session_id, current_user)
    except LegacyOwnershipError as exc:
        raise WorkflowError(
            str(exc),
            code=exc.code,
            status_code=exc.status_code,
            public_message=str(exc),
        ) from exc
    event_queue: Queue[str | None] = Queue()
    cancel_event = Event()
    trace_id = getattr(http_request.state, "trace_id", "")

    def emit_event(payload: dict) -> None:
        event_name = str(payload.get("event") or "message")
        serialized = json.dumps(payload, ensure_ascii=False)
        event_queue.put(f"event: {event_name}\ndata: {serialized}\n\n")

    def worker() -> None:
        try:
            artifact = _run_report_generation(
                service=service,
                request=request,
                current_user=current_user,
                capability_config_service=capability_config_service,
                progress_callback=emit_event,
                cancel_event=cancel_event,
            )
            ownership = persist_legacy_report_ownership(
                http_request,
                request,
                artifact,
                current_user,
                settings=getattr(service, "settings", None) or deps.get_settings(),
            )
            emit_event(
                {
                    "event": "final",
                    "payload": _build_generate_report_response(
                        artifact,
                        session_id=ownership.session_id,
                    ).model_dump(),
                }
            )
        except RequestCancelledError:
            if not cancel_event.is_set():
                emit_event(build_sse_error_event(
                    request=http_request,
                    code="REQUEST_CANCELLED",
                    message="客户端连接已断开，报告生成已取消。",
                    retryable=True,
                ))
        except LegacyOwnershipError as exc:
            emit_event(build_sse_error_event(
                request=http_request,
                code=exc.code,
                message=str(exc),
                retryable=exc.status_code >= 500,
            ))
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, WorkflowError):
                emit_event(build_sse_error_event(
                    request=http_request,
                    code=exc.code,
                    message=exc.public_message,
                    retryable=exc.retryable,
                    details=exc.details,
                ))
            else:
                emit_event(build_sse_error_event(
                    request=http_request,
                    code="INTERNAL_ERROR",
                    message="服务内部处理失败，请稍后重试；如持续失败，请联系维护者并提供错误追踪号。",
                    retryable=False,
                ))
        finally:
            event_queue.put(None)

    emit_event(
        {
            "event": "stage",
            "stage": "connect",
            "status": "started",
            "label": "已建立报告流，等待后端开始处理",
        }
    )
    worker_thread = Thread(target=worker, daemon=True)
    worker_thread.start()

    async def event_stream():
        last_stream_activity = monotonic()
        try:
            while True:
                if await http_request.is_disconnected():
                    cancel_event.set()
                    _cancel_report_service(service)
                    break

                try:
                    item = await asyncio.to_thread(
                        event_queue.get,
                        True,
                        REPORT_STREAM_QUEUE_POLL_SECONDS,
                    )
                except Empty:
                    if not worker_thread.is_alive() and event_queue.empty():
                        break
                    now = monotonic()
                    if now - last_stream_activity >= REPORT_STREAM_HEARTBEAT_SECONDS:
                        last_stream_activity = now
                        yield _build_sse_comment_frame("keepalive")
                    continue

                if item is None:
                    break
                last_stream_activity = monotonic()
                yield item
        finally:
            cancel_event.set()
            _cancel_report_service(service)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Trace-Id": trace_id,
        },
    )


def _generate_harness_report_stream(http_request: Request, run):  # noqa: ANN001
    async def event_stream():
        disconnected = False
        task = asyncio.create_task(run.execute())
        sequence = 0
        yield _serialize_sse_event({
            "event": "stage",
            "stage": "connect",
            "status": "started",
            "label": "已建立报告流，等待 harness 开始处理",
        })
        try:
            while True:
                if await http_request.is_disconnected():
                    disconnected = True
                    _cancel_harness_run(run)
                    break

                try:
                    page = await asyncio.to_thread(run.events, sequence)
                except WorkflowError as exc:
                    yield _serialize_sse_event(build_sse_error_event(
                        request=http_request,
                        code=exc.code,
                        message=exc.public_message,
                        retryable=exc.retryable,
                        details=exc.details,
                    ))
                    break
                except Exception:  # noqa: BLE001
                    yield _serialize_sse_event(build_sse_error_event(
                        request=http_request,
                        code="INTERNAL_ERROR",
                        message="服务内部处理失败，请稍后重试；如持续失败，请联系维护者并提供错误追踪号。",
                        retryable=False,
                    ))
                    break
                for event in page["events"]:
                    sequence = event["seq"]
                    payload = _build_harness_sse_event(event)
                    if payload is not None:
                        yield _serialize_sse_event(payload)

                if task.done():
                    try:
                        view = await task
                        page = await asyncio.to_thread(run.events, sequence)
                        for event in page["events"]:
                            sequence = event["seq"]
                            payload = _build_harness_sse_event(event)
                            if payload is not None:
                                yield _serialize_sse_event(payload)
                        response = build_legacy_response(run, view)
                        yield _serialize_sse_event({
                            "event": "final", "payload": response.model_dump(),
                        })
                    except RequestCancelledError:
                        if not disconnected:
                            yield _serialize_sse_event(build_sse_error_event(
                                request=http_request,
                                code="REQUEST_CANCELLED",
                                message="客户端连接已断开，报告生成已取消。",
                                retryable=True,
                            ))
                    except WorkflowError as exc:
                        yield _serialize_sse_event(build_sse_error_event(
                            request=http_request,
                            code=exc.code,
                            message=exc.public_message,
                            retryable=exc.retryable,
                            details=exc.details,
                        ))
                    except asyncio.CancelledError:
                        if not disconnected:
                            yield _serialize_sse_event(build_sse_error_event(
                                request=http_request,
                                code="REQUEST_CANCELLED",
                                message="报告生成已取消。",
                                retryable=True,
                            ))
                    except Exception:  # noqa: BLE001
                        yield _serialize_sse_event(build_sse_error_event(
                            request=http_request,
                            code="INTERNAL_ERROR",
                            message="服务内部处理失败，请稍后重试；如持续失败，请联系维护者并提供错误追踪号。",
                            retryable=False,
                        ))
                    break
                await asyncio.sleep(REPORT_STREAM_QUEUE_POLL_SECONDS)
        finally:
            if not task.done():
                _cancel_harness_run(run)
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Trace-Id": run.trace_id,
        },
    )


def _serialize_sse_event(payload: dict) -> str:
    event_name = str(payload.get("event") or "message")
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _build_harness_sse_event(event: dict) -> dict | None:
    event_type = str(event.get("type") or "")
    if event_type in {"final", "error"}:
        return None
    data = event.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    stage = str(data.get("stage") or data.get("step") or event_type or "processing")
    status = "completed" if event_type in {"checkpoint", "review"} else "started"
    return {
        "event": "stage",
        "stage": stage,
        "status": status,
        "label": stage,
    }


def _cancel_harness_run(run) -> None:  # noqa: ANN001
    try:
        run.cancel()
    except WorkflowError:
        # 终态或租约已被回收时不覆盖已有终态。
        pass


@router.get("/{trace_id}/exports/{export_format}")
def download_report_export(
    http_request: Request,
    trace_id: str,
    export_format: ReportExportFormat,
    cover_title: str | None = Query(default=None, max_length=48),
    cover_subtitle: str | None = Query(default=None, max_length=64),
    cover_compiled_by: str | None = Query(default=None, max_length=48),
    cover_date_mode: PdfCoverDateMode = Query(default="today"),
    cover_date_text: str | None = Query(default=None, max_length=32),
    current_user: AuthenticatedUser = Depends(get_current_user),
    harness_context: LegacyHarnessContext | None = Depends(get_legacy_harness_context),
    service: ReportExportService = Depends(get_report_export_service),
):
    harness_record = get_legacy_harness_export_record(
        harness_context, trace_id, current_user.id,
    )
    if harness_record is not None:
        assert harness_context is not None
        try:
            content, media_type, filename = render_run_export(
                harness_record,
                export_format,
                mode="formal",
                registry=harness_context.service.dependencies.release_registry,
                renderer=service,
            )
        except HarnessError as exc:
            raise WorkflowError(
                exc.code,
                code=exc.code,
                status_code=exc.status_code,
                details=exc.details,
                public_message={
                    "report_not_published": "报告尚未正式发布，未提供导出。",
                    "release_binding_revoked": "正式报告发布绑定已撤销，未提供导出。",
                }.get(exc.code, "正式报告导出失败，未提供文件。"),
            ) from exc
        # PDF/DOCX 渲染可能耗时；渲染完成后重新读取 owner-scoped 记录，
        # 防止渲染期间撤销批准后仍返回已生成内容。
        current_harness_record = get_legacy_harness_export_record(
            harness_context, trace_id, current_user.id,
        )
        if current_harness_record is None:
            raise WorkflowError(
                "报告发布状态已变化，未提供导出。",
                code="report_not_published",
                status_code=404,
                public_message="报告尚未正式发布，未提供导出。",
            )
        if not export_eligibility(
            current_harness_record,
            harness_context.service.dependencies.release_registry,
        )[0]:
            raise WorkflowError(
                "正式报告发布绑定已撤销，未提供导出。",
                code="release_binding_revoked",
                status_code=409,
                public_message="正式报告发布绑定已撤销，未提供导出。",
            )
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    settings = getattr(service, "settings", None)
    if settings is None:
        settings = deps.get_settings()
    if not is_legacy_export_authorized(
        http_request,
        trace_id,
        current_user,
        settings=settings,
    ):
        raise WorkflowError(
            "旧报告产物缺少当前用户的持久所有权绑定。",
            code="legacy_export_owner_required",
            status_code=404,
            public_message="报告产物不存在或当前账号无权访问。",
        )
    export_path = service.get_export_path(
        trace_id,
        export_format,
        pdf_cover_options=PdfCoverOptions(
            title=cover_title,
            subtitle=cover_subtitle,
            compiled_by=cover_compiled_by,
            date_mode=cover_date_mode,
            date_text=cover_date_text,
        )
        if export_format == "pdf"
        else None,
    )

    return FileResponse(
        path=export_path,
        media_type=service.get_media_type(export_format),
        filename=service.build_download_name(trace_id, export_format),
        headers={"Cache-Control": "no-store"},
    )


def _authorize_report_request(
    request: GenerateReportRequest,
    current_user: AuthenticatedUser | None,
) -> None:
    """报告生成会调用系统模型端点，必须登录；服务器文件路径可指向任何用户的资料，仅限管理员。"""
    if current_user is None:
        raise AuthenticationError("生成报告需要先登录。", code="AUTHENTICATION_REQUIRED")
    if (request.input_path or request.video_path) and not current_user.is_admin:
        raise PermissionDeniedError("只有管理员可以使用服务器文件路径作为报告输入。")


def _run_report_generation(
    service: ReportService,
    request: GenerateReportRequest,
    current_user: AuthenticatedUser | None = None,
    capability_config_service: UserCapabilityConfigService | None = None,
    progress_callback=None,  # noqa: ANN001
    cancel_event: Event | None = None,
):
    input_path = None
    video_path = None
    if request.input_path:
        input_path = str(
            resolve_api_path(
                service.settings,
                request.input_path,
                field_name="input_path",
                allowed_roots=[
                    service.settings.backend_data_dir_path,
                    service.settings.input_generation_workspace_dir_path,
                    service.settings.output_dir_path,
                ],
            )
        )
    if request.video_path:
        video_path = str(
            resolve_api_path(
                service.settings,
                request.video_path,
                field_name="video_path",
                allowed_roots=[
                    service.settings.backend_data_dir_path,
                    service.settings.input_generation_workspace_dir_path,
                    service.settings.resolve_path("backend/data/runtime/uploads"),
                ],
            )
        )
    capability_overrides = None
    if current_user is not None:
        if capability_config_service is None:
            raise RuntimeError("报告生成缺少模型能力配置服务。")
        # 普通用户：视觉/报告未配置会在此抛 InputValidationError（→400）；嵌入留空走系统默认。
        # 管理员：全部留空时返回 None，使用系统默认端点。
        capability_overrides = capability_config_service.resolve_overrides(current_user)

    return service.generate(
        session_id=request.session_id,
        input_path=input_path,
        accident_data=request.accident_data,
        video_path=video_path,
        persist_generated_input=request.persist_generated_input,
        persist_accident_data=request.persist_accident_data,
        capability_overrides=capability_overrides,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )


def _build_generate_report_response(
    artifact,
    *,
    session_id: str | None = None,
) -> GenerateReportResponse:  # noqa: ANN001
    retrieval_meta = dict(artifact.retrieval_meta or {})
    if session_id:
        retrieval_meta.setdefault("session_id", session_id)
    return GenerateReportResponse(
        trace_id=artifact.trace_id,
        status="success",
        output_dir=artifact.output_dir,
        guidance=artifact.guidance,
        report=artifact.report.model_dump(),
        input_generation=artifact.input_generation.model_dump() if artifact.input_generation else None,
        initial_knowledge_snippets=artifact.initial_knowledge_snippets,
        knowledge_snippets=artifact.knowledge_snippets,
        retrieval_meta=retrieval_meta,
        agentic_retrieval_rounds=artifact.agentic_retrieval_rounds,
    )


def _cancel_report_service(service: object) -> None:
    cancel = getattr(service, "cancel_active_run", None)
    if callable(cancel):
        cancel()


def _build_sse_comment_frame(comment: str) -> str:
    return f": {comment}\n\n"
