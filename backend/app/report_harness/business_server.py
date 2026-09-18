"""显式开发入口；复用原应用，不修改正式入口或取得正式导出资格。"""

from __future__ import annotations

import argparse
from pathlib import Path

from fastapi import Depends

from app.api.deps import get_database_service, get_settings
from app.api.routes_report_runs import get_report_run_service
from app.report_harness.business_bootstrap import (
    BusinessRuntimeManifest, assemble_business_runtime,
)
from app.report_harness.resources import check_host_memory, check_storage
from app.report_harness.store import RunStore
from app.services.report_run_service import ReportRunService


def install_business_runtime(app, runtime):
    if (not runtime.development_outbound_enabled or not runtime.force_engineering_exports
            or runtime.business_workflow is None):
        raise ValueError("本入口只允许完整业务开发依赖。")

    def service(database=Depends(get_database_service)):
        store = RunStore(database.connection)
        store.check_schema()
        return ReportRunService(store, runtime)

    app.dependency_overrides[get_report_run_service] = service
    return app


def main():
    parser = argparse.ArgumentParser(description="完整业务开发服务器；仍需逐运行确认外发。")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--resource-path", type=Path, action="append", required=True)
    parser.add_argument("--port", type=int, default=18282)
    parser.add_argument("--minimum-free-mib", type=int, default=2048)
    parser.add_argument("--minimum-memory-mib", type=int, default=256)
    parser.add_argument("--confirm-development-server", action="store_true")
    args = parser.parse_args()
    if not args.confirm_development_server:
        parser.error("必须明确启用开发入口。")
    if not 1 <= args.port <= 65535 or min(args.minimum_free_mib, args.minimum_memory_mib) <= 0:
        parser.error("端口和资源保留值不合法。")
    raw = args.manifest.read_bytes()
    if len(raw) > 1024 * 1024:
        parser.error("运行配置过大。")
    manifest = BusinessRuntimeManifest.model_validate_json(raw)
    paths = tuple(path.resolve(strict=True) for path in args.resource_path)
    ledger_parent = Path(manifest.ledger_path).resolve(strict=True).parent
    paths = (*paths, ledger_parent)

    def check_resources():
        check_storage(paths, minimum_free_bytes=args.minimum_free_mib * 1024 * 1024)
        check_host_memory(minimum_available_bytes=args.minimum_memory_mib * 1024 * 1024)

    check_resources()
    runtime = assemble_business_runtime(get_settings(), manifest, resource_check=check_resources)
    from app.main import app
    import uvicorn

    install_business_runtime(app, runtime)
    uvicorn.run(app, host="127.0.0.1", port=args.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
