import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.api import deps
from app.report_harness import lifecycle, production_runtime
from app.report_harness.config import ReportHarnessSettings
from app.report_harness.errors import HarnessError


@pytest.mark.parametrize("code", ["resource_pressure", "resource_probe_failed", "release_not_approved"])
def test_startup_only_degrades_for_resource_failures(monkeypatch, code):
    app = FastAPI()
    config = ReportHarnessSettings(enabled=True, online_enabled=True)
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(report_harness=config))
    app.dependency_overrides[deps.get_database_service] = lambda: SimpleNamespace(connection=None)
    monkeypatch.setattr(lifecycle, "RunStore",
                        lambda connection: SimpleNamespace(check_schema=lambda: None))
    monkeypatch.setattr(lifecycle, "RunRecovery", lambda store: None)

    def reject(*args):
        raise HarnessError(code, 503)

    async def reconcile(recovery, stop):
        await stop.wait()

    monkeypatch.setattr(production_runtime, "assemble_production_runtime", reject)
    monkeypatch.setattr(lifecycle, "reconcile_loop", reconcile)

    async def check():
        async with lifecycle.report_harness_lifespan(app):
            assert app.state.report_harness_runtime is None
            assert app.state.report_harness_blocked_reason == code

    if code == "release_not_approved":
        with pytest.raises(HarnessError, match=code):
            asyncio.run(check())
    else:
        asyncio.run(check())
        assert app.state.report_harness_blocked_reason is None
