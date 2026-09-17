from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api import deps
from app.core.security import create_access_token
from app.main import app
from app.report_harness.authorization import EndpointDescription
from app.report_harness.config import ReportHarnessSettings
from app.report_harness.contracts import canonical_digest
from app.report_harness.test_database import validate_test_dsn
from tests.test_report_run_auth import _prepare_auth_users, _synthetic_settings


def test_real_main_factory_enables_data_paths_but_blocks_outbound(pg_store, monkeypatch):
    import os

    store, owner, other, session = pg_store
    dsn = validate_test_dsn(os.environ["REPORT_HARNESS_TEST_DSN"])
    _prepare_auth_users(dsn, owner, other)
    settings = _synthetic_settings(dsn)
    settings.report_harness = ReportHarnessSettings(
        enabled=True,
        endpoints=[
            EndpointDescription(role=role, label="合成端点", base_url="https://example.invalid/v1",
                                model="synthetic", version="v1")
            for role in ("generator", "reviewer")
        ],
        approved_knowledge_manifests=[canonical_digest([])],
    )
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    # 此用例仅测工厂接线；真实批准表ACL由独立测试验证，不更改生产文件权限。
    monkeypatch.setattr(
        "app.api.routes_report_runs.FileReleaseRegistry",
        lambda: SimpleNamespace(validate=lambda: None, status=lambda _binding: "unapproved"),
    )
    app.dependency_overrides[deps.get_database_service] = lambda: SimpleNamespace(connection=store.connection)
    token = create_access_token(auth_settings=settings.auth, user_id=owner, username=owner, role="user")
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with TestClient(app) as client:
            path = f"/api/v1/chat-sessions/{session}/report-evidence"
            assert client.get(path, headers=headers).json()["revision"] == 0
            saved = client.put(path, headers=headers, json={
                "expected_revision": 0, "records": [{
                    "evidence_id": str(uuid4()), "text": "合成补证",
                    "source_label": "测试", "source_locator": "第一段",
                    "kind": "statement", "verification_status": "unverified",
                }],
            })
            assert saved.status_code == 200
            response = client.post("/api/v1/report-runs", headers=headers, json={
                "request_id": str(uuid4()), "session_id": session,
                "accident_data": {"事实": "合成"}, "evidence_revision": 1,
            })
            assert response.status_code == 201
            run_id = response.json()["run_id"]
            preview = client.get(f"/api/v1/report-runs/{run_id}/authorization-preview",
                                 headers=headers).json()
            assert preview["available"]
            assert preview["snapshot"]["supplemental_records"][0]["text"] == "合成补证"
            approval = {
                name: preview[name] for name in (
                    "snapshot_digest", "endpoint_profile_digest", "approved_knowledge_manifest_digest",
                )
            }
            approved = client.post(f"/api/v1/report-runs/{run_id}/authorize", headers=headers,
                                   json={**approval, "confirmed": True})
            assert approved.status_code == 200
            rejected = client.post(f"/api/v1/report-runs/{run_id}/execute/stream", headers=headers,
                                   json={"expected_version": approved.json()["state_version"]})
            assert rejected.status_code == 503
            assert rejected.json()["error"]["code"] == "outbound_transport_unavailable"
            assert client.get(f"/api/v1/report-runs/{run_id}", headers=headers).json()["state"] == "queued"
    finally:
        app.dependency_overrides.pop(deps.get_database_service, None)
        with store.connection() as conn:
            conn.execute("DELETE FROM users WHERE username=%s", (settings.auth.bootstrap_admin_username,))
