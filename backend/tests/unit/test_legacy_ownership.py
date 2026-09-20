from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api import deps
from app.api.deps import get_authed_chat_session_service
from app.api.routes_chat_sessions import router as chat_sessions_router
from app.report_harness.legacy_ownership import (
    LEGACY_OWNERSHIP_FILENAME,
    LegacyOwnershipError,
    assert_legacy_session_access,
    is_legacy_export_authorized,
    persist_legacy_report_ownership,
)
from app.schemas.chat_session import ChatSessionRecord
from app.schemas.workflow import GenerateReportRequest
from app.services.auth_service import AuthenticatedUser


class _Result:
    def __init__(self, *, rows=None, row=None):
        self._rows = list(rows or [])
        self._row = row

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, *, report_rows=None, session_row=None, fail_report_lookup=False, fail=False):
        self.report_rows = list(report_rows or [])
        self.session_row = session_row
        self.fail_report_lookup = fail_report_lookup
        self.fail = fail
        self.statements: list[str] = []

    def execute(self, statement, params=()):
        normalized = " ".join(str(statement).lower().split())
        self.statements.append(normalized)
        if self.fail or (self.fail_report_lookup and "report_result is not null" in normalized):
            raise RuntimeError("合成数据库故障")
        if "report_result is not null" in normalized:
            return _Result(rows=self.report_rows)
        return _Result(row=self.session_row)

    @contextmanager
    def transaction(self):
        yield self


class _Database:
    def __init__(self, connection):
        self._connection = connection

    @contextmanager
    def connection(self):
        yield self._connection


def _user(user_id: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        id=user_id,
        username=user_id,
        display_name=None,
        role="user",
        is_active=True,
        created_at="",
        updated_at="",
    )


def _request(database: object) -> Request:
    return _request_factory(lambda: database)


def _request_factory(factory) -> Request:  # noqa: ANN001
    app = FastAPI()
    app.dependency_overrides[deps.get_database_service] = factory
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "app": app,
    })


def _settings(tmp_path: Path):
    return SimpleNamespace(output_dir_path=tmp_path)


def _make_output_dir(tmp_path: Path, trace_id: str) -> Path:
    output_dir = tmp_path / trace_id
    output_dir.mkdir()
    (output_dir / "report.md").write_text("合成报告", encoding="utf-8")
    return output_dir


def _artifact(output_dir: Path, trace_id: str):
    return SimpleNamespace(
        trace_id=trace_id,
        output_dir=str(output_dir),
        guidance={},
        report={},
    )


def _generation_request() -> GenerateReportRequest:
    return GenerateReportRequest(accident_data={"事实": "合成报告"})


def _write_sidecar(output_dir: Path, trace_id: str, owner_user_id: str) -> None:
    (output_dir / LEGACY_OWNERSHIP_FILENAME).write_text(
        json.dumps({
            "version": 1,
            "trace_id": trace_id,
            "owner_user_id": owner_user_id,
            "owner_username": owner_user_id,
            "session_id": "trusted-session",
            "output_dir": str(output_dir.resolve()),
        }),
        encoding="utf-8",
    )


def test_chat_session_http_accepts_client_report_result_on_create_and_put():
    class CapturingService:
        def __init__(self):
            self.requests = []

        @staticmethod
        def _record(report_result):
            return ChatSessionRecord(
                id="session-client-controlled",
                owner_user_id="owner",
                owner_username="owner",
                created_at=1,
                updated_at=2,
                report_result=report_result,
            )

        def create_session(self, request):
            self.requests.append(("create", request))
            return self._record(request.report_result)

        def update_session(self, session_id, request):
            self.requests.append(("update", request))
            return self._record(request.report_result)

    service = CapturingService()
    app = FastAPI()
    app.include_router(chat_sessions_router)
    app.dependency_overrides[get_authed_chat_session_service] = lambda: service

    payload = {"report_result": {"trace_id": "foreign-trace", "output_dir": "/foreign"}}
    with TestClient(app) as client:
        created = client.post("/api/v1/chat-sessions", json=payload)
        updated = client.put("/api/v1/chat-sessions/session-client-controlled", json=payload)

    assert created.status_code == 200
    assert updated.status_code == 200
    assert [kind for kind, _ in service.requests] == ["create", "update"]
    assert all(request.report_result == payload["report_result"] for _, request in service.requests)


def test_trusted_sidecar_precedes_forged_report_result_owner(tmp_path):
    trace_id = "sidecar-priority-trace"
    output_dir = _make_output_dir(tmp_path, trace_id)
    _write_sidecar(output_dir, trace_id, "owner")
    connection = _Connection(report_rows=[{
        "id": "attacker-session",
        "owner_user_id": "attacker",
        "owner_username": "attacker",
        "report_result": {"trace_id": trace_id},
    }])
    request = _request(_Database(connection))

    assert is_legacy_export_authorized(
        request, trace_id, _user("owner"), settings=_settings(tmp_path),
    ) is True
    assert is_legacy_export_authorized(
        request, trace_id, _user("attacker"), settings=_settings(tmp_path),
    ) is False


def test_history_without_sidecar_uses_server_run_log_session_owner(tmp_path):
    trace_id = "run-log-owner-trace"
    output_dir = _make_output_dir(tmp_path, trace_id)
    (output_dir / "run_log.json").write_text(
        json.dumps({"trace_id": trace_id, "session_id": "trusted-session"}),
        encoding="utf-8",
    )
    connection = _Connection(
        report_rows=[
            {"id": "attacker-latest", "owner_user_id": "attacker", "owner_username": "attacker"},
            {"id": "owner-older", "owner_user_id": "owner", "owner_username": "owner"},
        ],
        session_row={
            "id": "trusted-session",
            "owner_user_id": "owner",
            "owner_username": "owner",
            "report_result": {"trace_id": "attacker-controlled"},
        },
        fail_report_lookup=True,
    )
    request = _request(_Database(connection))

    assert is_legacy_export_authorized(
        request, trace_id, _user("owner"), settings=_settings(tmp_path),
    ) is True
    assert is_legacy_export_authorized(
        request, trace_id, _user("attacker"), settings=_settings(tmp_path),
    ) is False
    assert not any("report_result is not null" in statement for statement in connection.statements)


def test_legacy_session_access_fails_closed_on_database_error():
    class BrokenDatabase:
        @contextmanager
        def connection(self):
            raise RuntimeError("合成数据库故障")
            yield

    with pytest.raises(LegacyOwnershipError) as error:
        assert_legacy_session_access(
            _request(BrokenDatabase()), "foreign-session", _user("owner"),
        )

    assert error.value.code == "legacy_ownership_unavailable"
    assert error.value.status_code == 503


def test_legacy_session_access_rejects_missing_explicit_session():
    with pytest.raises(LegacyOwnershipError) as error:
        assert_legacy_session_access(
            _request(_Database(_Connection(session_row=None))),
            "missing-session",
            _user("owner"),
        )

    assert error.value.code == "legacy_session_owner_required"
    assert error.value.status_code == 404


def test_new_legacy_ownership_fails_closed_when_database_factory_raises(tmp_path):
    trace_id = "database-factory-error-trace"
    output_dir = _make_output_dir(tmp_path, trace_id)

    def broken_factory():
        raise RuntimeError("合成数据库工厂故障")

    with pytest.raises(LegacyOwnershipError) as error:
        persist_legacy_report_ownership(
            _request_factory(broken_factory),
            _generation_request(),
            _artifact(output_dir, trace_id),
            _user("owner"),
            settings=_settings(tmp_path),
        )

    assert error.value.code == "legacy_ownership_unavailable"
    assert error.value.status_code == 503
    assert not (output_dir / LEGACY_OWNERSHIP_FILENAME).exists()


def test_new_legacy_ownership_rejects_session_missing_during_generation(tmp_path):
    trace_id = "session-missing-during-generation-trace"
    output_dir = _make_output_dir(tmp_path, trace_id)

    with pytest.raises(LegacyOwnershipError) as error:
        persist_legacy_report_ownership(
            _request(_Database(_Connection(session_row=None))),
            GenerateReportRequest(
                accident_data={"事实": "合成报告"},
                session_id="missing-session",
            ),
            _artifact(output_dir, trace_id),
            _user("owner"),
            settings=_settings(tmp_path),
        )

    assert error.value.code == "legacy_session_owner_required"
    assert error.value.status_code == 404
    assert not (output_dir / LEGACY_OWNERSHIP_FILENAME).exists()


def test_new_legacy_ownership_fails_closed_when_database_write_raises(tmp_path):
    trace_id = "database-write-error-trace"
    output_dir = _make_output_dir(tmp_path, trace_id)

    with pytest.raises(LegacyOwnershipError) as error:
        persist_legacy_report_ownership(
            _request(_Database(_Connection(fail=True))),
            _generation_request(),
            _artifact(output_dir, trace_id),
            _user("owner"),
            settings=_settings(tmp_path),
        )

    assert error.value.code == "legacy_export_owner_persistence_failed"
    assert error.value.status_code == 503
    assert not (output_dir / LEGACY_OWNERSHIP_FILENAME).exists()


def test_explicitly_unconfigured_database_keeps_sidecar_compatibility(tmp_path):
    trace_id = "database-unconfigured-trace"
    output_dir = _make_output_dir(tmp_path, trace_id)

    result = persist_legacy_report_ownership(
        _request_factory(lambda: None),
        _generation_request(),
        _artifact(output_dir, trace_id),
        _user("owner"),
        settings=_settings(tmp_path),
    )

    assert result.source == "output_sidecar"
    assert (output_dir / LEGACY_OWNERSHIP_FILENAME).exists()


def test_trusted_sidecar_remains_readable_when_database_factory_raises(tmp_path):
    trace_id = "sidecar-database-failure-trace"
    output_dir = _make_output_dir(tmp_path, trace_id)
    _write_sidecar(output_dir, trace_id, "owner")
    called = False

    def broken_factory():
        nonlocal called
        called = True
        raise RuntimeError("合成数据库暂时不可用")

    assert is_legacy_export_authorized(
        _request_factory(broken_factory),
        trace_id,
        _user("owner"),
        settings=_settings(tmp_path),
    ) is True
    assert called is False


def test_legacy_export_denies_database_error_without_sidecar(tmp_path):
    trace_id = "database-error-trace"
    _make_output_dir(tmp_path, trace_id)
    connection = _Connection(fail=True)

    assert is_legacy_export_authorized(
        _request(_Database(connection)),
        trace_id,
        _user("owner"),
        settings=_settings(tmp_path),
    ) is False


def test_postgres_run_log_owner_wins_over_client_report_result(pg_store, tmp_path):
    store, owner, other, session_id = pg_store
    trace_id = "postgres-owner-conflict-" + uuid4().hex
    attacker_session_id = "client-report-" + uuid4().hex
    output_dir = _make_output_dir(tmp_path, trace_id)
    (output_dir / "run_log.json").write_text(
        json.dumps({"trace_id": trace_id, "session_id": session_id}),
        encoding="utf-8",
    )

    try:
        with store.connection() as conn, conn.transaction():
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS report_result jsonb")
            conn.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS updated_at bigint DEFAULT 0")
            conn.execute(
                """
                insert into chat_sessions(
                    id, owner_user_id, owner_username, report_result, updated_at
                ) values (%s, %s, %s, %s::jsonb, %s)
                """,
                (
                    attacker_session_id,
                    other,
                    other,
                    json.dumps({"trace_id": trace_id, "output_dir": str(output_dir.resolve())}),
                    2,
                ),
            )
            conn.execute(
                """
                update chat_sessions
                set report_result=%s::jsonb, updated_at=%s
                where id=%s
                """,
                (
                    json.dumps({"trace_id": trace_id, "output_dir": str(output_dir.resolve())}),
                    1,
                    session_id,
                ),
            )

        request = _request(SimpleNamespace(connection=store.connection))
        assert is_legacy_export_authorized(
            request, trace_id, _user(owner), settings=_settings(tmp_path),
        ) is True
        assert is_legacy_export_authorized(
            request, trace_id, _user(other), settings=_settings(tmp_path),
        ) is False
    finally:
        with store.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM chat_sessions WHERE id=%s", (attacker_session_id,))
