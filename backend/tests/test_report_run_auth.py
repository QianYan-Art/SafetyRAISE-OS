from __future__ import annotations

import os
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from app.api import deps
from app.api.routes_report_runs import get_report_run_service
from app.core.security import create_access_token
from app.core.settings import AuthSettings, DatabaseSettings
from app.main import app
from app.report_harness.test_database import validate_test_dsn
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles, dependencies


def _prepare_auth_users(dsn: str, owner: str, other: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        conn.execute(
            """
            ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name text;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS role text;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active boolean;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at timestamptz;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at timestamptz;
            """
        )
        conn.execute("ALTER TABLE users ALTER COLUMN id SET DEFAULT gen_random_uuid()")
        conn.execute(
            """
            UPDATE users
            SET password_hash = %s,
                display_name = username,
                role = 'user',
                is_active = TRUE,
                created_at = COALESCE(created_at, now()),
                updated_at = COALESCE(updated_at, now())
            WHERE id IN (%s, %s)
            """,
            ("unused-test-hash", owner, other),
        )


def _synthetic_settings(dsn: str) -> SimpleNamespace:
    return SimpleNamespace(
        auth=AuthSettings(
            jwt_secret="report-run-auth-test-secret-0123456789",
            bootstrap_admin_username=f"bootstrap-{uuid4().hex}",
            bootstrap_admin_password="unused-test-password",
            bootstrap_admin_display_name="测试引导账号",
        ),
        database=DatabaseSettings(dsn=dsn),
    )


def test_report_run_http_uses_real_jwt_auth_and_postgres(pg_store, monkeypatch):
    store, owner, other, session = pg_store
    dsn = validate_test_dsn(os.environ["REPORT_HARNESS_TEST_DSN"])
    _prepare_auth_users(dsn, owner, other)
    settings = _synthetic_settings(dsn)
    database = SimpleNamespace(connection=store.connection)
    roles = SyntheticRoles()
    service = ReportRunService(store, dependencies(roles))
    owner_token = create_access_token(
        auth_settings=settings.auth,
        user_id=owner,
        username=owner,
        role="user",
    )
    other_token = create_access_token(
        auth_settings=settings.auth,
        user_id=other,
        username=other,
        role="user",
    )
    payload = {
        "request_id": str(uuid4()),
        "session_id": session,
        "accident_data": {"事实": "真实 PG 认证入口合成样本"},
        "evidence_revision": 0,
    }

    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app.dependency_overrides[deps.get_database_service] = lambda: database
    app.dependency_overrides[get_report_run_service] = lambda: service
    try:
        with TestClient(app) as client:
            assert client.post("/api/v1/report-runs", json=payload).status_code == 401

            owner_headers = {"Authorization": f"Bearer {owner_token}"}
            other_headers = {"Authorization": f"Bearer {other_token}"}
            created = client.post(
                "/api/v1/report-runs", headers=owner_headers, json=payload
            )
            assert created.status_code == 201
            run_id = created.json()["run_id"]
            assert roles.calls == []

            assert client.get(
                f"/api/v1/report-runs/{run_id}", headers=other_headers
            ).status_code == 404
            assert client.get(
                f"/api/v1/report-runs/{run_id}", headers=owner_headers
            ).json()["state"] == "queued"

            executed = client.post(
                f"/api/v1/report-runs/{run_id}/execute/stream",
                headers=owner_headers,
                json={"expected_version": 0},
            )
            assert executed.status_code == 200
            assert '"state": "published"' in executed.text
            assert roles.calls == ["prepare", "generate", "review"]

            final = client.get(
                f"/api/v1/report-runs/{run_id}", headers=owner_headers
            )
            assert final.status_code == 200
            assert final.json()["state"] == "published"
            assert final.json()["review_status"] == "passed"
    finally:
        app.dependency_overrides.pop(deps.get_database_service, None)
        app.dependency_overrides.pop(get_report_run_service, None)
        with psycopg.connect(dsn) as conn:
            conn.execute("DELETE FROM users WHERE username=%s",
                         (settings.auth.bootstrap_admin_username,))
