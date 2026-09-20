from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from app.report_harness.recovery import RunRecovery
from app.report_harness.store import RunStore
from app.report_harness.errors import HarnessError

logger = logging.getLogger(__name__)


async def reconcile_loop(recovery: RunRecovery, stop: asyncio.Event,
                         interval: float = 5.0) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(recovery.sweep_expired)
        except Exception:
            # 不输出异常内容，避免连接参数进入日志；下一周期重新检测。
            logger.error("报告运行本地对账失败；新模式仍按 schema 与租约检查拒绝不安全操作。")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass


@asynccontextmanager
async def report_harness_lifespan(app):
    from app.api.deps import get_database_service, get_settings
    from app.report_harness.config import ReportHarnessSettings

    settings = get_settings()
    config = ReportHarnessSettings.model_validate(
        getattr(settings, "report_harness", ReportHarnessSettings())
    )
    app.state.report_harness_runtime = None
    app.state.report_harness_blocked_reason = None
    if not config.enabled:
        yield
        return
    database_factory = app.dependency_overrides.get(get_database_service, get_database_service)
    database = database_factory()
    if config.online_enabled:
        from app.report_harness.production_runtime import assemble_production_runtime

        RunStore(database.connection).check_schema()
        try:
            app.state.report_harness_runtime = assemble_production_runtime(settings, config)
        except HarnessError as exc:
            if exc.code not in {"resource_pressure", "resource_probe_failed"}:
                raise
            app.state.report_harness_blocked_reason = exc.code
            logger.warning("报告生成因资源不足暂不可用，保留历史读取和取消入口。")
    stop = asyncio.Event()
    task = asyncio.create_task(
        reconcile_loop(RunRecovery(RunStore(database.connection)), stop)
    )
    try:
        yield
    finally:
        app.state.report_harness_runtime = None
        app.state.report_harness_blocked_reason = None
        stop.set()
        # 等待当前数据库事务结束，不在结算中途取消后台线程。
        await task
