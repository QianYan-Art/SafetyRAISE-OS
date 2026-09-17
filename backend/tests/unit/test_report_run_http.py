import asyncio
import json
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from app.api.deps import get_current_user
from app.api.routes_report_runs import ReportRunBodyLimit, get_report_run_service, router
from app.services.auth_service import AuthenticatedUser
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import MemoryStore, SyntheticRoles, dependencies


def http_app(store=None, profile="synthetic_test", roles=None):
    roles = roles or SyntheticRoles()
    service = ReportRunService(store or MemoryStore(), dependencies(roles, profile))
    app = FastAPI()
    app.add_middleware(ReportRunBodyLimit)
    app.include_router(router)

    def synthetic_identity(authorization: str | None = Header(None)):
        if authorization not in {"Bearer owner", "Bearer other"}:
            raise HTTPException(401, "仅供合成测试的身份")
        return AuthenticatedUser(
            id=authorization.split()[1], username=authorization.split()[1],
            display_name=None, role="user", is_active=True, created_at="", updated_at="",
        )

    app.dependency_overrides[get_current_user] = synthetic_identity
    app.dependency_overrides[get_report_run_service] = lambda: service
    return app, service, roles


def payload():
    return {
        "request_id": str(uuid4()), "session_id": "synthetic-session",
        "accident_data": {"事实": "仅合成测试"}, "evidence_revision": 0,
    }


def test_http_synthetic_flow_and_owner_gate():
    app, _, roles = http_app()
    with TestClient(app) as client:
        assert client.post("/api/v1/report-runs", json=payload()).status_code == 401
        headers = {"Authorization": "Bearer owner"}
        created = client.post("/api/v1/report-runs", headers=headers, json=payload())
        assert created.status_code == 201
        run_id = created.json()["run_id"]
        assert roles.calls == []
        assert client.get(f"/api/v1/report-runs/{run_id}", headers={
            "Authorization": "Bearer other",
        }).status_code == 404
        result = client.post(
            f"/api/v1/report-runs/{run_id}/execute/stream", headers=headers,
            json={"expected_version": 0},
        )
        assert result.status_code == 200
        assert '"state": "published"' in result.text
        view = client.get(f"/api/v1/report-runs/{run_id}", headers=headers).json()
        assert view["state"] == "published"
        assert view["review_status"] == "passed"
        assert roles.calls.count("generate") == roles.calls.count("review") == 1
        repeated = client.post(
            f"/api/v1/report-runs/{run_id}/execute/stream", headers=headers,
            json={"expected_version": view["state_version"]},
        )
        assert repeated.status_code == 409
        assert roles.calls.count("generate") == 1


def test_http_outbound_is_rejected_before_sse_starts():
    app, _, roles = http_app(profile="outbound")
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer owner"}
        created = client.post("/api/v1/report-runs", headers=headers, json=payload()).json()
        response = client.post(
            f"/api/v1/report-runs/{created['run_id']}/execute/stream",
            headers=headers, json={"expected_version": 0},
        )
        assert response.status_code == 409
        assert response.json()["detail"] == "authorization_required"
        assert response.json()["error"]["code"] == "authorization_required"
        assert "trace_id" in response.json()["error"]
        assert roles.calls == []


def test_http_rejects_client_profile_and_oversized_body():
    app, _, roles = http_app()
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer owner"}
        illegal = {**payload(), "execution_profile": "synthetic_test"}
        assert client.post("/api/v1/report-runs", headers=headers, json=illegal).status_code == 422
        assert client.post(
            "/api/v1/report-runs", headers=headers, content=b"x" * (256 * 1024 + 1),
        ).status_code == 413
        assert roles.calls == []


def test_production_dependency_stays_disabled():
    app, _, _ = http_app()
    app.dependency_overrides.pop(get_report_run_service)
    with TestClient(app) as client:
        response = client.post("/api/v1/report-runs", json=payload(),
                               headers={"Authorization": "Bearer owner"})
        assert response.status_code == 503


def test_asgi_stream_disconnect_persists_cancellation():
    class WaitingRoles(SyntheticRoles):
        async def generate(self, context):
            await asyncio.Event().wait()

    app, service, _ = http_app(roles=WaitingRoles())
    from app.schemas.report_run import CreateRunRequest
    run = service.create("owner", CreateRunRequest.model_validate(payload()))

    async def disconnect():
        sent_request = False
        response_started = asyncio.Event()

        async def receive():
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": json.dumps({
                    "expected_version": 0,
                }).encode(), "more_body": False}
            await response_started.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                response_started.set()

        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "scheme": "http", "method": "POST",
            "path": f"/api/v1/report-runs/{run['run_id']}/execute/stream",
            "query_string": b"", "root_path": "",
            "headers": [(b"authorization", b"Bearer owner"), (b"content-type", b"application/json")],
            "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1234),
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=3)

    asyncio.run(disconnect())
    assert service.get("owner", run["run_id"])["state"] == "cancelled"


def test_failed_role_emits_persisted_structured_error():
    app, service, _ = http_app(roles=SyntheticRoles(fail_generate=True))
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer owner"}
        run = client.post("/api/v1/report-runs", json=payload(), headers=headers).json()
        response = client.post(
            f"/api/v1/report-runs/{run['run_id']}/execute/stream",
            json={"expected_version": 0}, headers=headers,
        )
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    errors = [frame for frame in frames if frame["type"] == "error"]
    assert len(errors) == 1
    assert set(errors[0]) == {"run_id", "seq", "type", "state_version", "occurred_at", "data"}
    assert service.get("owner", run["run_id"])["state"] == "failed"
