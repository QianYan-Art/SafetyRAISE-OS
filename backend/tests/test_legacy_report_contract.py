from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import (
    get_optional_current_user,
    get_report_service,
    get_user_capability_config_service,
)
from app.api.routes_report import router
from app.core.exceptions import RequestCancelledError
from app.schemas.report import ReportResult


class LegacyReportStub:
    """旧接口测试替身，不创建模型或导出客户端。"""

    def __init__(self, wait_for_disconnect: bool = False):
        self.wait_for_disconnect = wait_for_disconnect
        self.calls: list[dict] = []
        self.cancel_calls = 0
        self.finished = threading.Event()

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        try:
            if self.wait_for_disconnect:
                cancel_event = kwargs["cancel_event"]
                while not cancel_event.is_set():
                    cancel_event.wait(0.01)
                raise RequestCancelledError("测试客户端已断开")
            return SimpleNamespace(
                trace_id="trace-legacy-test",
                output_dir="test-output",
                guidance={"source": "stub"},
                report=ReportResult(report_markdown="旧接口合成报告正文"),
                input_generation=None,
                initial_knowledge_snippets=[],
                knowledge_snippets=[],
                retrieval_meta={},
                agentic_retrieval_rounds=[],
            )
        finally:
            self.finished.set()

    def cancel_active_run(self):
        self.cancel_calls += 1


def legacy_app(service: LegacyReportStub) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_report_service] = lambda: service
    app.dependency_overrides[get_optional_current_user] = lambda: None
    app.dependency_overrides[get_user_capability_config_service] = lambda: object()
    return app


def legacy_payload() -> dict:
    return {
        "accident_data": {"事实": "仅用于旧接口合成回归"},
        "persist_generated_input": False,
    }


def test_legacy_anonymous_generate_and_stream_remain_usable():
    service = LegacyReportStub()
    app = legacy_app(service)

    with TestClient(app) as client:
        generated = client.post("/api/v1/reports/generate", json=legacy_payload())
        streamed = client.post("/api/v1/reports/generate/stream", json=legacy_payload())

    assert generated.status_code == 200
    assert generated.json()["status"] == "success"
    assert generated.json()["report"]["report_markdown"] == "旧接口合成报告正文"
    assert streamed.status_code == 200
    assert "event: final" in streamed.text
    assert '"status": "success"' in streamed.text
    assert len(service.calls) == 2
    assert all(call["capability_overrides"] is None for call in service.calls)


def test_legacy_stream_disconnect_cancels_generation():
    service = LegacyReportStub(wait_for_disconnect=True)
    app = legacy_app(service)

    async def disconnect():
        sent_request = False
        response_started = asyncio.Event()

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {
                    "type": "http.request",
                    "body": json.dumps(legacy_payload()).encode(),
                    "more_body": False,
                }
            await response_started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                response_started.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "http",
            "method": "POST",
            "path": "/api/v1/reports/generate/stream",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"application/json")],
            "server": ("127.0.0.1", 80),
            "client": ("127.0.0.1", 1234),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=3)

    asyncio.run(disconnect())
    assert service.cancel_calls >= 1
    assert service.finished.wait(1)
