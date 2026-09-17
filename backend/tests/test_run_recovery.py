"""07 故障恢复门：真实 PostgreSQL、真实子进程和回环合成 transport。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.routes_report_runs import get_report_run_service
from app.core.security import create_access_token
from app.core.settings import AuthSettings, DatabaseSettings
from app.main import app
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.recovery import RunRecovery
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.execution import ReportExecutionDependencies
from app.report_harness.store import RunStore
from app.schemas.report_run import BudgetPolicy, CreateRunRequest
from app.services.report_run_service import ReportRunService
from app.report_harness.test_database import validate_test_dsn
from tests.harness_crash_worker import (
    ENDPOINT_PROFILE_DIGEST,
    KNOWLEDGE_MANIFEST_DIGEST,
    POLICY_DIGEST,
    PROOF_DIGEST,
    WORKER_PATH,
)
from tests.harness_fixtures import SyntheticRoles


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"


def _policy() -> BudgetPolicy:
    return BudgetPolicy(
        max_physical_requests=8,
        max_tool_calls=8,
        max_revision_rounds=0,
        max_retrieval_requests=8,
        max_active_seconds=600,
        max_total_tokens=1600,
        max_output_tokens_per_request=20,
    )


def _dependencies(policy: BudgetPolicy) -> ReportExecutionDependencies:
    return ReportExecutionDependencies(
        roles_factory=lambda: SyntheticRoles(),
        execution_profile="synthetic_test",
        endpoint_profile_digest=ENDPOINT_PROFILE_DIGEST,
        policy_digest=POLICY_DIGEST,
        knowledge_manifest_digest=KNOWLEDGE_MANIFEST_DIGEST,
        budget_policy=policy,
    )


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
        conn.execute(
            """
            UPDATE users
            SET password_hash = %s,
                display_name = username,
                role = 'user',
                is_active = TRUE,
                created_at = COALESCE(created_at, now()),
                updated_at = now()
            WHERE id IN (%s, %s)
            """,
            ("unused-crash-worker-hash", owner, other),
        )


@contextmanager
def _api_client(pg_store, service: ReportRunService):
    store, owner, other, _session = pg_store
    dsn = validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    _prepare_auth_users(dsn, owner, other)
    settings = SimpleNamespace(
        auth=AuthSettings(
            jwt_secret="report-run-recovery-test-secret-0123456789",
            bootstrap_admin_username="unused-recovery-bootstrap",
            bootstrap_admin_password="unused-recovery-password",
            bootstrap_admin_display_name="恢复测试账号",
        ),
        database=DatabaseSettings(dsn=dsn),
    )
    database = SimpleNamespace(connection=store.connection)
    owner_token = create_access_token(
        auth_settings=settings.auth, user_id=owner, username=owner, role="user",
    )
    old_settings = deps.get_settings
    old_database = app.dependency_overrides.get(deps.get_database_service)
    old_service = app.dependency_overrides.get(get_report_run_service)
    deps.get_settings = lambda: settings
    app.dependency_overrides[deps.get_database_service] = lambda: database
    app.dependency_overrides[get_report_run_service] = lambda: service
    try:
        with TestClient(app) as client:
            yield client, {"Authorization": f"Bearer {owner_token}"}
    finally:
        deps.get_settings = old_settings
        if old_database is None:
            app.dependency_overrides.pop(deps.get_database_service, None)
        else:
            app.dependency_overrides[deps.get_database_service] = old_database
        if old_service is None:
            app.dependency_overrides.pop(get_report_run_service, None)
        else:
            app.dependency_overrides[get_report_run_service] = old_service


@contextmanager
def _model_server(*, first_generator_tool: bool):
    calls: list[dict] = []
    lock = threading.Lock()
    state = {"tool_sent": False}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            role = str(payload.get("model", "")).removeprefix("synthetic-")
            context = json.loads(payload["messages"][1]["content"])
            with lock:
                calls.append({"role": role, "payload": payload, "context": context})
            if (
                role == "generator"
                and first_generator_tool
                and not context.get("tool_results")
                and not state["tool_sent"]
            ):
                state["tool_sent"] = True
                body = {
                    "choices": [{
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [{
                                "id": "synthetic-list-evidence",
                                "function": {
                                    "name": "list_evidence",
                                    "arguments": "{}",
                                },
                            }],
                        },
                    }],
                    "usage": {"total_tokens": 200},
                }
            else:
                roles = SyntheticRoles()
                result = asyncio.run(
                    roles.generate(context) if role == "generator"
                    else roles.review(context)
                )
                body = {
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(result, ensure_ascii=False),
                        },
                    }],
                    "usage": {"total_tokens": 200},
                }
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/synthetic", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _worker_env() -> dict[str, str]:
    # Windows 的 asyncio/_overlapped 需要完整的宿主进程环境；worker 仍只使用
    # 命令行里的 DSN/端点，且主动移除测试 DSN，避免环境变量成为隐式输入。
    env = dict(os.environ)
    env.pop("REPORT_HARNESS_TEST_DSN", None)
    env["PYTHONPATH"] = os.pathsep.join((str(BACKEND_ROOT), str(BACKEND_ROOT / "tests")))
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _start_worker(
    *,
    dsn: str,
    endpoint: str,
    owner: str,
    run_id: str,
    action: str,
    expected_version: int = 0,
    crash_at: str | None = None,
    marker: Path | None = None,
    retry_unknown_requests: bool = False,
) -> subprocess.Popen:
    command = [
        sys.executable,
        "-u",
        str(WORKER_PATH),
        "--action",
        action,
        "--dsn",
        dsn,
        "--endpoint",
        endpoint,
        "--owner",
        owner,
        "--run-id",
        run_id,
        "--expected-version",
        str(expected_version),
    ]
    if marker is not None:
        command.extend(["--marker", str(marker)])
    if crash_at is not None:
        command.extend(["--crash-at", crash_at])
    if retry_unknown_requests:
        command.append("--retry-unknown-requests")
    return subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=_worker_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )


def _wait_marker(process: subprocess.Popen, marker: Path, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            try:
                return json.loads(marker.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            raise AssertionError(
                f"worker 在 marker 前退出: {process.returncode}; {stdout}; {stderr}"
            )
        time.sleep(0.05)
    raise AssertionError(f"未等到 worker marker: {marker}")


def _kill_and_wait(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)
    assert process.poll() is not None
    process.communicate(timeout=2)


def _cleanup_processes(*processes: subprocess.Popen | None) -> None:
    for process in processes:
        if process is not None:
            _kill_and_wait(process) if process.poll() is None else process.communicate(timeout=2)


def _finish(process: subprocess.Popen) -> dict:
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, f"worker 失败: {stderr}\n{stdout}"
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, f"worker 没有结果: {stderr}"
    return json.loads(lines[-1])


def _create_run(client: TestClient, headers: dict, session: str) -> str:
    response = client.post(
        "/api/v1/report-runs",
        headers=headers,
        json={
            "request_id": str(uuid4()),
            "session_id": session,
            "accident_data": {"事实": "07 子进程崩溃恢复合成样本"},
            "evidence_revision": 0,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


def _expire_and_recover(store: RunStore, owner: str, run_id: str) -> dict:
    # 这里只缩短测试等待，不把真实生产 30 秒租约改写成“立即过期”的运行语义。
    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE run_id=%s",
            (run_id,),
        )
    return RunRecovery(store).recover_expired(owner, run_id)


def _request_rows(store: RunStore, run_id: str) -> list[dict]:
    with store.connection() as conn:
        return conn.execute(
            "SELECT request_id,role,status,reserved_tokens,actual_tokens,result "
            "FROM report_run_requests WHERE run_id=%s ORDER BY request_id",
            (run_id,),
        ).fetchall()


def test_kill_after_tool_checkpoint_reuses_tool_and_model_result(pg_store, tmp_path):
    store, owner, _other, session = pg_store
    service = ReportRunService(store, _dependencies(_policy()))
    dsn = validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    with _api_client(pg_store, service) as (client, headers), _model_server(
        first_generator_tool=True
    ) as (endpoint, calls):
        run_id = _create_run(client, headers, session)
        marker = tmp_path / "tool.json"
        worker = _start_worker(
            dsn=dsn, endpoint=endpoint, owner=owner, run_id=run_id,
            action="execute", expected_version=0,
            crash_at="tool_checkpoint", marker=marker,
        )
        resumed = None
        try:
            assert _wait_marker(worker, marker)["marker"] == "tool_checkpoint"
            _kill_and_wait(worker)
            assert len(calls) == 1

            recovered = _expire_and_recover(store, owner, run_id)
            assert recovered["state"] == "suspended"
            before = RequestLedger(store).view(owner, run_id)
            assert before["known_used"] == 200
            assert before["unknown_reserved"] == 0

            resumed = _start_worker(
                dsn=dsn, endpoint=endpoint, owner=owner, run_id=run_id,
                action="resume", expected_version=recovered["state_version"],
            )
            assert _finish(resumed)["result"]["state"] == "published"
            assert [item["role"] for item in calls] == [
                "generator", "generator", "reviewer",
            ]
            events = store.events(owner, run_id)["events"]
            tool_events = [
                item for item in events
                if item["type"] == "tool"
                and item["data"].get("name") == "list_evidence"
                and item["data"].get("status") == "completed"
            ]
            assert len(tool_events) == 1
            budget = RequestLedger(store).view(owner, run_id)
            assert budget["physical_requests"] == 3
            assert budget["known_used"] == 600
        finally:
            _cleanup_processes(worker, resumed)


def test_http_response_before_settle_default_409_then_explicit_retry_keeps_reserve(
    pg_store, tmp_path,
):
    store, owner, _other, session = pg_store
    service = ReportRunService(store, _dependencies(_policy()))
    dsn = validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    with _api_client(pg_store, service) as (client, headers), _model_server(
        first_generator_tool=False
    ) as (endpoint, calls):
        run_id = _create_run(client, headers, session)
        marker = tmp_path / "response.json"
        worker = _start_worker(
            dsn=dsn, endpoint=endpoint, owner=owner, run_id=run_id,
            action="execute", expected_version=0,
            crash_at="http_response_before_settle", marker=marker,
        )
        resumed = None
        try:
            assert _wait_marker(worker, marker)["marker"] == "http_response_before_settle"
            _kill_and_wait(worker)
            assert len(calls) == 1
            recovered = _expire_and_recover(store, owner, run_id)
            assert recovered["state"] == "suspended"
            before = RequestLedger(store).view(owner, run_id)
            assert before["unknown_reserved"] == 200

            rejected = client.post(
                f"/api/v1/report-runs/{run_id}/resume/stream",
                headers=headers,
                json={"expected_version": recovered["state_version"]},
            )
            assert rejected.status_code == 409
            assert len(calls) == 1
            assert client.get(
                f"/api/v1/report-runs/{run_id}", headers=headers
            ).json()["state"] == "suspended"

            resumed = _start_worker(
                dsn=dsn, endpoint=endpoint, owner=owner, run_id=run_id,
                action="resume", expected_version=recovered["state_version"],
                retry_unknown_requests=True,
            )
            assert _finish(resumed)["result"]["state"] == "published"
            assert [item["role"] for item in calls] == [
                "generator", "generator", "reviewer",
            ]
            rows = _request_rows(store, run_id)
            assert sum(row["status"] == "completion_unknown" for row in rows) == 1
            budget = RequestLedger(store).view(owner, run_id)
            assert budget["physical_requests"] == 3
            assert budget["unknown_reserved"] == 200
            assert budget["known_used"] == 400
        finally:
            _cleanup_processes(worker, resumed)


def test_settle_before_journal_reuses_response_without_duplicate_http(pg_store, tmp_path):
    store, owner, _other, session = pg_store
    service = ReportRunService(store, _dependencies(_policy()))
    dsn = validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    with _api_client(pg_store, service) as (client, headers), _model_server(
        first_generator_tool=False
    ) as (endpoint, calls):
        run_id = _create_run(client, headers, session)
        marker = tmp_path / "settle.json"
        worker = _start_worker(
            dsn=dsn, endpoint=endpoint, owner=owner, run_id=run_id,
            action="execute", expected_version=0,
            crash_at="settle_before_journal", marker=marker,
        )
        resumed = None
        try:
            assert _wait_marker(worker, marker)["marker"] == "settle_before_journal"
            _kill_and_wait(worker)
            assert len(calls) == 1
            recovered = _expire_and_recover(store, owner, run_id)
            resumed = _start_worker(
                dsn=dsn, endpoint=endpoint, owner=owner, run_id=run_id,
                action="resume", expected_version=recovered["state_version"],
            )
            assert _finish(resumed)["result"]["state"] == "published"
            assert [item["role"] for item in calls] == ["generator", "reviewer"]
            budget = RequestLedger(store).view(owner, run_id)
            assert budget["physical_requests"] == 2
            assert budget["known_used"] == 400
        finally:
            _cleanup_processes(worker, resumed)


@pytest.mark.parametrize(
    "crash_at,expected_initial_calls,expected_roles",
    [
        ("candidate_checkpoint", 2, ["generator", "generator", "reviewer"]),
        ("review_checkpoint", 3, ["generator", "generator", "reviewer"]),
    ],
)
def test_candidate_and_review_checkpoints_resume_without_replaying_completed_http(
    pg_store, tmp_path, crash_at, expected_initial_calls, expected_roles,
):
    store, owner, _other, session = pg_store
    service = ReportRunService(store, _dependencies(_policy()))
    dsn = validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    with _api_client(pg_store, service) as (client, headers), _model_server(
        first_generator_tool=True
    ) as (endpoint, calls):
        run_id = _create_run(client, headers, session)
        marker = tmp_path / f"{crash_at}.json"
        worker = _start_worker(
            dsn=dsn, endpoint=endpoint, owner=owner, run_id=run_id,
            action="execute", expected_version=0,
            crash_at=crash_at, marker=marker,
        )
        resumed = None
        try:
            assert _wait_marker(worker, marker)["marker"] == crash_at
            _kill_and_wait(worker)
            assert len(calls) == expected_initial_calls
            recovered = _expire_and_recover(store, owner, run_id)
            resumed = _start_worker(
                dsn=dsn, endpoint=endpoint, owner=owner, run_id=run_id,
                action="resume", expected_version=recovered["state_version"],
            )
            assert _finish(resumed)["result"]["state"] == "published"
            assert [item["role"] for item in calls] == expected_roles
        finally:
            _cleanup_processes(worker, resumed)


def test_published_checkpoint_restarts_as_read_only_without_http(pg_store, tmp_path):
    store, owner, _other, session = pg_store
    service = ReportRunService(store, _dependencies(_policy()))
    dsn = validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    with _api_client(pg_store, service) as (client, headers), _model_server(
        first_generator_tool=True
    ) as (endpoint, calls):
        run_id = _create_run(client, headers, session)
        marker = tmp_path / "published.json"
        worker = _start_worker(
            dsn=dsn, endpoint=endpoint, owner=owner, run_id=run_id,
            action="execute", expected_version=0,
            crash_at="published_checkpoint", marker=marker,
        )
        replay = None
        try:
            assert _wait_marker(worker, marker)["marker"] == "published_checkpoint"
            _kill_and_wait(worker)
            assert len(calls) == 3
            replay = _start_worker(
                dsn=dsn, endpoint=endpoint, owner=owner, run_id=run_id,
                action="replay",
            )
            assert _finish(replay)["state"] == "published"
            read_back = client.get(f"/api/v1/report-runs/{run_id}", headers=headers)
            assert read_back.status_code == 200
            assert read_back.json()["state"] == "published"
            assert len(calls) == 3
            events = store.events(owner, run_id)["events"]
            assert sum(item["type"] == "final" for item in events) == 1
            publication = next(item for item in events if item["type"] == "final")
            frozen = store.get(owner, run_id)["publication_budget"]
            assert publication["data"]["budget"] == frozen
            assert frozen["physical_requests"] == 3
            assert frozen["known_used"] == 600
            assert frozen["unknown_reserved"] == 0
            assert frozen["active_seconds"] >= 0
        finally:
            _cleanup_processes(worker, replay)


def test_cancel_and_delete_fence_old_worker_and_block_recovery(pg_store):
    store, owner, _other, session = pg_store
    service = ReportRunService(store, _dependencies(_policy()))
    cancelled = service.create(
        owner,
        CreateRunRequest(
            request_id=uuid4(), session_id=session,
            accident_data={"事实": "取消恢复边界"}, evidence_revision=0,
        ),
    )
    token = service.claim(owner, cancelled["run_id"], 0)
    store.cancel(owner, cancelled["run_id"], expected_token=token)
    with pytest.raises(HarnessError, match="lease_lost"):
        store.assert_active(owner, cancelled["run_id"], token)
    assert RunRecovery(store).recover_expired(owner, cancelled["run_id"])["state"] == "cancelled"

    deleted_session = "harness-recovery-deleted-" + str(uuid4())
    deleted_run = None
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO chat_sessions(id,owner_user_id) VALUES (%s,%s)",
            (deleted_session, owner),
        )
    try:
        deleted_run = service.create(
            owner,
            CreateRunRequest(
                request_id=uuid4(), session_id=deleted_session,
                accident_data={"事实": "删除恢复边界"}, evidence_revision=0,
            ),
        )
        deleted_token = service.claim(owner, deleted_run["run_id"], 0)
        with store.connection() as conn:
            conn.execute(
                "INSERT INTO session_deletion_barriers(session_id) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (deleted_session,),
            )
        with pytest.raises(HarnessError, match="not_found"):
            RunRecovery(store).recover_expired(owner, deleted_run["run_id"])
        with pytest.raises(HarnessError):
            store.assert_active(owner, deleted_run["run_id"], deleted_token)
    finally:
        with store.connection() as conn, conn.transaction():
            if deleted_run is not None:
                conn.execute(
                    "DELETE FROM report_run_events WHERE run_id=%s", (deleted_run["run_id"],)
                )
                conn.execute(
                    "DELETE FROM report_run_requests WHERE run_id=%s", (deleted_run["run_id"],)
                )
                conn.execute(
                    "DELETE FROM report_runs WHERE run_id=%s", (deleted_run["run_id"],)
                )
            conn.execute(
                "DELETE FROM session_deletion_barriers WHERE session_id=%s", (deleted_session,)
            )
            conn.execute("DELETE FROM chat_sessions WHERE id=%s", (deleted_session,))


def test_resume_validation_failure_is_atomic_after_real_crash(pg_store, tmp_path):
    store, owner, _other, session = pg_store
    service = ReportRunService(store, _dependencies(_policy()))
    dsn = validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    with _api_client(pg_store, service) as (client, headers), _model_server(
        first_generator_tool=False
    ) as (endpoint, calls):
        run_id = _create_run(client, headers, session)
        marker = tmp_path / "validation.json"
        worker = _start_worker(
            dsn=dsn, endpoint=endpoint, owner=owner, run_id=run_id,
            action="execute", expected_version=0,
            crash_at="http_response_before_settle", marker=marker,
        )
        try:
            _wait_marker(worker, marker)
            _kill_and_wait(worker)
            before = _expire_and_recover(store, owner, run_id)
            before_row = store.get(owner, run_id)
            # 该断言直接走恢复模块，验证失败不得改变 suspended 行的版本、租约和正文。
            with pytest.raises(ValueError, match="版本不一致"):
                RunRecovery(store).resume_claim(
                    owner, run_id, before["state_version"], uuid4(),
                    retry_unknown_requests=True,
                    validate=lambda _value: (_ for _ in ()).throw(ValueError("版本不一致")),
                    minimum_requests=0,
                    minimum_tokens=0,
                )
            assert store.get(owner, run_id) == before_row
            assert len(calls) == 1
        finally:
            _cleanup_processes(worker)
