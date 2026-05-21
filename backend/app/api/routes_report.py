import asyncio
import json
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import get_report_export_service, get_report_service
from app.api.error_handling import build_sse_error_event
from app.core.exceptions import RequestCancelledError, WorkflowError
from app.core.path_guard import resolve_api_path
from app.schemas.workflow import GenerateReportRequest, GenerateReportResponse
from app.services.report_export_service import PdfCoverDateMode, PdfCoverOptions, ReportExportFormat, ReportExportService
from app.services.report_service import ReportService

router = APIRouter(prefix="/api/v1/reports", tags=["reports"])
REPORT_STREAM_QUEUE_POLL_SECONDS = 0.2
REPORT_STREAM_HEARTBEAT_SECONDS = 15.0


@router.post("/generate", response_model=GenerateReportResponse)
def generate_report(
    request: GenerateReportRequest,
    service: ReportService = Depends(get_report_service),
):
    artifact = _run_report_generation(service=service, request=request)
    return _build_generate_report_response(artifact)


@router.post("/generate/stream")
def generate_report_stream(
    http_request: Request,
    request: GenerateReportRequest,
    service: ReportService = Depends(get_report_service),
):
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
                progress_callback=emit_event,
                cancel_event=cancel_event,
            )
            emit_event(
                {
                    "event": "final",
                    "payload": _build_generate_report_response(artifact).model_dump(),
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


@router.get("/{trace_id}/exports/{export_format}")
def download_report_export(
    trace_id: str,
    export_format: ReportExportFormat,
    cover_title: str | None = Query(default=None, max_length=48),
    cover_subtitle: str | None = Query(default=None, max_length=64),
    cover_compiled_by: str | None = Query(default=None, max_length=48),
    cover_date_mode: PdfCoverDateMode = Query(default="today"),
    cover_date_text: str | None = Query(default=None, max_length=32),
    service: ReportExportService = Depends(get_report_export_service),
):
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


def _run_report_generation(
    service: ReportService,
    request: GenerateReportRequest,
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
    return service.generate(
        session_id=request.session_id,
        input_path=input_path,
        accident_data=request.accident_data,
        video_path=video_path,
        persist_generated_input=request.persist_generated_input,
        persist_accident_data=request.persist_accident_data,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )


def _build_generate_report_response(artifact) -> GenerateReportResponse:  # noqa: ANN001
    return GenerateReportResponse(
        trace_id=artifact.trace_id,
        status="success",
        output_dir=artifact.output_dir,
        guidance=artifact.guidance,
        report=artifact.report.model_dump(),
        input_generation=artifact.input_generation.model_dump() if artifact.input_generation else None,
        initial_knowledge_snippets=artifact.initial_knowledge_snippets,
        knowledge_snippets=artifact.knowledge_snippets,
        retrieval_meta=artifact.retrieval_meta,
        agentic_retrieval_rounds=artifact.agentic_retrieval_rounds,
    )


def _cancel_report_service(service: object) -> None:
    cancel = getattr(service, "cancel_active_run", None)
    if callable(cancel):
        cancel()


def _build_sse_comment_frame(comment: str) -> str:
    return f": {comment}\n\n"
