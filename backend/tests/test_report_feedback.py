import os
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from app.api import deps
from app.api.routes_report_feedback import get_feedback_store
from app.api.routes_report_runs import get_report_run_service
from app.core.security import create_access_token
from app.core.settings import AuthSettings, DatabaseSettings
from app.main import app
from app.report_harness.feedback import FeedbackStore
from app.report_harness.test_database import validate_test_dsn
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles, dependencies
from tests.test_run_recovery import _prepare_auth_users


@contextmanager
def _clients(pg_store, roles=None):
    """owner 为普通用户；other 提升为管理员，但不是运行所有者。"""
    store, owner, other, _session = pg_store
    dsn = validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    _prepare_auth_users(dsn, owner, other)
    with psycopg.connect(dsn) as conn:
        conn.execute("UPDATE users SET role='admin' WHERE id=%s", (other,))
    settings = SimpleNamespace(
        auth=AuthSettings(
            jwt_secret="report-feedback-test-secret-0123456789abcdef",
            bootstrap_admin_username="unused-feedback-bootstrap",
            bootstrap_admin_password="unused-feedback-password",
            bootstrap_admin_display_name="反馈测试账号",
        ),
        database=DatabaseSettings(dsn=dsn),
    )
    service = ReportRunService(store, dependencies(roles or SyntheticRoles()))
    overrides = {
        deps.get_database_service: lambda: SimpleNamespace(connection=store.connection),
        get_report_run_service: lambda: service,
        get_feedback_store: lambda: FeedbackStore(store),
    }
    previous = {key: app.dependency_overrides.get(key) for key in overrides}
    old_settings = deps.get_settings
    deps.get_settings = lambda: settings
    app.dependency_overrides.update(overrides)

    def headers(user_id, role):
        token = create_access_token(auth_settings=settings.auth, user_id=user_id,
                                    username=user_id, role=role)
        return {"Authorization": f"Bearer {token}"}

    try:
        with TestClient(app) as client:
            yield client, headers(owner, "user"), headers(other, "admin")
    finally:
        deps.get_settings = old_settings
        for key, value in previous.items():
            if value is None:
                app.dependency_overrides.pop(key, None)
            else:
                app.dependency_overrides[key] = value


def _finished_run(client, headers, session) -> str:
    created = client.post("/api/v1/report-runs", headers=headers, json={
        "request_id": str(uuid4()), "session_id": session,
        "accident_data": {"事实": "反馈合成案例"}, "evidence_revision": 0,
    }).json()
    response = client.post(f"/api/v1/report-runs/{created['run_id']}/execute/stream", headers=headers,
                           json={"expected_version": created["state_version"]})
    assert response.status_code == 200
    return created["run_id"]


FEEDBACK = {"reviewer_name": " 王老师 ", "verdict": "needs_revision",
            "issue_tags": ["liability", "legal_citation"], "comment": "第三部分责任划分依据不足。"}


def test_owner_feedback_appends_revisions_with_optimistic_concurrency(pg_store):
    _store, _owner, _other, session = pg_store
    with _clients(pg_store) as (client, owner_headers, admin_headers):
        run_id = _finished_run(client, owner_headers, session)
        path = f"/api/v1/report-runs/{run_id}/feedback"
        assert client.get(path, headers=owner_headers).json() == {"run_id": run_id, "revision": 0}

        first = client.put(path, headers=owner_headers, json={"expected_revision": 0, **FEEDBACK})
        assert first.status_code == 200, first.text
        assert first.json()["revision"] == 1 and first.json()["reviewer_name"] == "王老师"

        stale = client.put(path, headers=owner_headers, json={"expected_revision": 0, **FEEDBACK})
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "feedback_revision_conflict"

        second = client.put(path, headers=owner_headers, json={
            **FEEDBACK, "expected_revision": 1, "verdict": "usable", "issue_tags": [],
        })
        assert second.json()["revision"] == 2
        assert client.get(path, headers=owner_headers).json()["verdict"] == "usable"

        # 管理员也只能经管理员汇总查看他人反馈，不能冒充所有者读写。
        assert client.get(path, headers=admin_headers).status_code == 404
        for invalid in ({"issue_tags": ["liability", "liability"]}, {"reviewer_name": "  "},
                        {"verdict": "great"}):
            bad = client.put(path, headers=owner_headers,
                             json={**FEEDBACK, "expected_revision": 2, **invalid})
            assert bad.status_code == 422


def test_admin_sees_latest_revision_with_frozen_context_and_exports_csv(pg_store):
    _store, _owner, _other, session = pg_store
    with _clients(pg_store) as (client, owner_headers, admin_headers):
        run_id = _finished_run(client, owner_headers, session)
        path = f"/api/v1/report-runs/{run_id}/feedback"
        client.put(path, headers=owner_headers, json={"expected_revision": 0, **FEEDBACK})
        client.put(path, headers=owner_headers, json={**FEEDBACK, "expected_revision": 1,
                                                       "comment": "补充：引用条文版本需核对。"})

        assert client.get("/api/v1/admin/report-feedback", headers=owner_headers).status_code == 403
        listing = client.get("/api/v1/admin/report-feedback", headers=admin_headers).json()
        items = [item for item in listing["items"] if item["run_id"] == run_id]
        assert len(items) == 1 and items[0]["revision"] == 2
        assert items[0]["comment"] == "补充：引用条文版本需核对。"
        context = items[0]["run_context"]
        assert context["state"] == "published" and context["quality_gate"] == "engineering_only"
        assert context["session_id"] == session and len(context["policy_digest"]) == 64

        def listed(**params):
            page = client.get("/api/v1/admin/report-feedback", headers=admin_headers, params=params)
            return run_id in {item["run_id"] for item in page.json()["items"]}

        assert listed(verdict="needs_revision") and not listed(verdict="usable")
        assert listed(tag="liability") and not listed(tag="format")

        exported = client.get("/api/v1/admin/report-feedback/export", headers=admin_headers)
        assert exported.status_code == 200
        assert exported.content.startswith("\ufeff".encode("utf-8"))
        text = exported.content.decode("utf-8-sig")
        assert text.splitlines()[0].startswith("反馈时间,反馈人,账号,报告编号")
        row = next(line for line in text.splitlines() if run_id in line)
        assert "修改后可用" in row and "责任认定不当、法规引用错误或过时" in row


def test_feedback_accepts_unreviewed_candidates(pg_store):
    _store, _owner, _other, session = pg_store
    with _clients(pg_store, SyntheticRoles(invalid_review=True)) as (client, owner_headers, _admin):
        run_id = _finished_run(client, owner_headers, session)
        view = client.get(f"/api/v1/report-runs/{run_id}", headers=owner_headers).json()
        assert view["state"] == "needs_review" and view["export_kind"] == "unreviewed"
        saved = client.put(f"/api/v1/report-runs/{run_id}/feedback", headers=owner_headers,
                           json={"expected_revision": 0, **FEEDBACK, "verdict": "unusable"})
        assert saved.status_code == 200 and saved.json()["verdict"] == "unusable"


def test_pending_migrations_are_idempotent(pg_store):
    from app.report_harness.schema_migrations import SCHEMA_VERSIONS, applied_versions, apply_pending

    store, *_ = pg_store
    with store.connection() as conn, conn.transaction():
        assert apply_pending(conn) == []
        assert applied_versions(conn) == set(SCHEMA_VERSIONS)
    store.check_schema()
