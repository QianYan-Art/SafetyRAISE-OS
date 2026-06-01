import argparse
import os
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.deps import get_readiness_service
from app.api.error_handling import register_exception_handlers
from app.api.routes_admin import router as admin_router
from app.api.routes_app_config import router as app_config_router
from app.api.routes_auth import router as auth_router
from app.api.routes_chat_sessions import router as chat_session_router
from app.api.routes_input import router as input_router
from app.api.routes_report import router as report_router
from app.api.routes_user_model_configs import router as user_model_config_router
from app.core.logger import setup_logging
from app.core.request_context import reset_trace_id, set_trace_id
from app.core.settings import load_settings
from app.services.readiness_service import ReadinessService
from app.services.report_service import ReportService

app = FastAPI(title="交通事故分析报告后端", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(app_config_router)
app.include_router(admin_router)
app.include_router(auth_router)
app.include_router(report_router)
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
    return response


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "mode": "liveness"}


@app.get("/api/v1/ready")
def ready(
    readiness_service: ReadinessService = Depends(get_readiness_service),
):
    payload = readiness_service.check()
    status_code = 200 if payload.get("ready") else 503
    return JSONResponse(status_code=status_code, content=payload)


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
        if settings.auth.require_strong_secret and settings.auth.jwt_secret_is_insecure():
            raise SystemExit(
                "拒绝启动：AUTH_JWT_SECRET 仍为公开默认串或为空。"
                "生产环境必须设置高熵随机密钥（如 `openssl rand -hex 32`），"
                "否则任何人可伪造管理员 token。"
            )
        uvicorn.run("app.main:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
