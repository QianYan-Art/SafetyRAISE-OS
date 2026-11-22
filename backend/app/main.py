import argparse
import json
import logging
import os
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import get_optional_current_user, get_readiness_service, get_settings
from app.api.error_handling import build_error_response, register_exception_handlers
from app.api.routes_admin import router as admin_router
from app.api.routes_app_config import router as app_config_router
from app.api.routes_auth import router as auth_router
from app.api.routes_chat_sessions import router as chat_session_router
from app.api.routes_input import router as input_router
from app.api.routes_report import router as report_router
from app.api.routes_report_runs import ReportRunBodyLimit, router as report_run_router
from app.api.routes_report_evidence import router as report_evidence_router
from app.api.routes_report_feedback import (
    admin_router as report_feedback_admin_router,
    router as report_feedback_router,
)
from app.api.routes_report_run_exports import router as report_run_exports_router
from app.api.routes_user_model_configs import router as user_model_config_router
from app.core.exceptions import PermissionDeniedError
from app.core.logger import setup_logging
from app.core.request_context import reset_trace_id, set_trace_id
from app.core.settings import load_settings
from app.services.auth_service import AuthenticatedUser
from app.services.readiness_service import ReadinessService
from app.services.report_service import ReportService
from app.report_harness.lifecycle import report_harness_lifespan

logger = logging.getLogger(__name__)
# 就绪接口无需登录：对外只给状态与固定提示，异常详情、服务器路径、内部端点与模型名只写日志。
_READINESS_PUBLIC_KEYS = frozenset({"status", "ready", "checks", "ok", "message"})
_DEFAULT_DEV_CORS_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"]
_AUTH_BODY_LIMIT_BYTES = 64 * 1024
_STANDARD_JSON_BODY_LIMIT_BYTES = 2 * 1024 * 1024


class JsonRequestBodyLimit:
    """对非上传的 API JSON 路由在解析前执行实际字节上限控制，防止超大请求体耗尽内存。"""

    def __init__(self, app):
        self.app = app

    @staticmethod
    def _resolve_limit(path: str) -> int | None:
        if path.startswith("/api/v1/auth/"):
            return _AUTH_BODY_LIMIT_BYTES
        if path == "/api/v1/inputs/generate-from-upload":
            return None
        if path.startswith("/api/v1/report-runs") or (
            path.startswith("/api/v1/chat-sessions/") and path.endswith("/report-evidence")
        ):
            # 仍交由更严格的 ReportRunBodyLimit（256 KiB）处理。
            return None
        if path.startswith("/api/v1/"):
            return _STANDARD_JSON_BODY_LIMIT_BYTES
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self._resolve_limit(scope.get("path", ""))
        if limit is None:
            return await self.app(scope, receive, send)
        if scope.get("method", "GET") in {"GET", "HEAD", "OPTIONS", "DELETE"}:
            return await self.app(scope, receive, send)

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                return await build_error_response(
                    request=Request(scope),
                    status_code=413,
                    code="request_too_large",
                    message="请求体超过允许大小。",
                    retryable=False,
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


def _resolve_cors_origins() -> list[str]:
    try:
        settings = get_settings()
    except Exception:  # noqa: BLE001
        return list(_DEFAULT_DEV_CORS_ORIGINS)
    configured = [origin.strip() for origin in settings.app.cors_allow_origins if str(origin or "").strip()]
    if configured:
        return configured
    if settings.app.env == "prod":
        return []
    return list(_DEFAULT_DEV_CORS_ORIGINS)


app = FastAPI(title="交通事故分析报告后端", version="0.3.13",
              lifespan=report_harness_lifespan)
app.add_middleware(JsonRequestBodyLimit)
app.add_middleware(ReportRunBodyLimit)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_resolve_cors_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Trace-Id"],
)
app.include_router(app_config_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(report_router)
app.include_router(report_run_router)
app.include_router(report_run_exports_router)
app.include_router(report_evidence_router)
app.include_router(report_feedback_router)
app.include_router(report_feedback_admin_router)
app.include_router(input_router)
app.include_router(chat_session_router)
app.include_router(user_model_config_router)
register_exception_handlers(app)


@app.middleware("http")
async def attach_trace_id(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id", "").strip() or f"trace-{uuid4().hex[:12]}"
    request.state.trace_id = trace_id
    token = set_trace_id(trace_id)
    try:
        response = await call_next(request)
    finally:
        reset_trace_id(token)
    response.headers["X-Trace-Id"] = trace_id
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "mode": "liveness"}


@app.get("/api/v1/ready")
def ready(
    probe: str = Query(default="passive"),
    readiness_service: ReadinessService = Depends(get_readiness_service),
    current_user: AuthenticatedUser | None = Depends(get_optional_current_user),
):
    active_probes = isinstance(probe, str) and probe.strip().lower() == "active"
    if active_probes:
        if not isinstance(current_user, AuthenticatedUser) or not current_user.is_admin:
            raise PermissionDeniedError("只有管理员可以触发主动外部依赖探测。")
        payload = readiness_service.check(active_external_probes=True)
    else:
        try:
            payload = readiness_service.check(active_external_probes=False)
        except TypeError:
            # 兼容仅实现无参 check() 的测试替身。
            payload = readiness_service.check()
    ready_ok = bool(payload.get("ready"))
    if not ready_ok:
        logger.warning("就绪检查未通过：%s", json.dumps(payload, ensure_ascii=False, default=str))
    return JSONResponse(status_code=200 if ready_ok else 503, content=_public_readiness(payload))


def _public_readiness(value):
    """顶层与每项检查只保留白名单字段；checks 的键是检查项名称，原样保留并递归处理。"""
    if not isinstance(value, dict):
        return value
    public = {key: item for key, item in value.items() if key in _READINESS_PUBLIC_KEYS}
    checks = public.get("checks")
    if isinstance(checks, dict):
        public["checks"] = {name: _public_readiness(item) for name, item in checks.items()}
    return public


def main() -> None:
    parser = argparse.ArgumentParser(description="交通事故分析报告后端")
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="执行一次报告生成")
    run_cmd.add_argument("--config", default=None, help="配置文件路径")
    run_cmd.add_argument("--input", default=None, help="事故输入 JSON 路径")
    run_cmd.add_argument(
        "--video",
        default=None,
        help="事故视频路径，传入后会先生成 backend/data/input_accident.json",
    )
    run_cmd.add_argument(
        "--no-persist-generated-input",
        action="store_true",
        help="仅在本次运行中使用视频生成的事故 JSON，不覆盖默认 backend/data/input_accident.json",
    )

    input_cmd = sub.add_parser(
        "generate-input",
        help="根据事故视频生成 backend/data/input_accident.json",
    )
    input_cmd.add_argument("--config", default=None, help="配置文件路径")
    input_cmd.add_argument("--video", required=True, help="事故视频路径")
    input_cmd.add_argument(
        "--no-persist-generated-input",
        action="store_true",
        help="仅输出工作目录中的 generated_input.json，不覆盖默认 backend/data/input_accident.json",
    )

    serve_cmd = sub.add_parser("serve", help="启动 API 服务")
    serve_cmd.add_argument("--config", default=None, help="配置文件路径")
    serve_cmd.add_argument("--host", default="0.0.0.0", help="监听地址")
    serve_cmd.add_argument("--port", type=int, default=8000, help="监听端口")

    args = parser.parse_args()
    if args.config:
        os.environ["WORKFLOW_CONFIG_PATH"] = args.config

    settings = load_settings(args.config)
    setup_logging(settings.app.log_level)

    if args.command == "run":
        artifact = ReportService(settings).generate(
            input_path=args.input,
            video_path=args.video,
            persist_generated_input=not args.no_persist_generated_input,
        )
        print("trace_id:", artifact.trace_id)
        print("output_dir:", artifact.output_dir)
        if artifact.input_generation:
            print("input_workspace_dir:", artifact.input_generation.workspace_dir)
            print("input_path:", artifact.input_generation.input_path)
        return

    if args.command == "generate-input":
        service = ReportService(settings)._build_input_generation_service()
        artifact = service.generate(
            video_path=args.video,
            persist_generated_input=not args.no_persist_generated_input,
        )
        print("input_path:", artifact.input_path)
        print("workspace_dir:", artifact.workspace_dir)
        if artifact.backup_path:
            print("backup_path:", artifact.backup_path)
        return

    if args.command == "serve":
        problems = settings.auth.startup_security_problems()
        if problems:
            raise SystemExit("拒绝启动：" + "；".join(problems) + "。")
        uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
