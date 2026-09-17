import asyncio
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from time import monotonic, sleep
from uuid import uuid4

import httpx
import pytest
import uvicorn
from psycopg import sql

from app.api.routes_report_runs import get_report_run_service
from app.main import app
from app.report_harness.errors import HarnessError
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.session_deletion import delete_session
from app.schemas.report_run import BudgetPolicy
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles, dependencies
from tests.test_report_tool_policy import tool_client
from tests.test_request_ledger import create_run, reserve


class WaitingRoles(SyntheticRoles):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.closed = threading.Event()

    async def generate(self, context):
        self.entered.set()
        await asyncio.Event().wait()

    async def close(self):
        self.closed.set()


@contextmanager
def loopback_app():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(
        app, log_level="error", lifespan="off", access_log=False,
    ))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=True,
    )
    thread.start()
    try:
        deadline = monotonic() + 5
        while not server.started:
            assert thread.is_alive() and monotonic() < deadline, "隔离HTTP服务未启动"
            sleep(0.01)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        assert not thread.is_alive()


def waiting_run(fixture):
    client, store, owner, session = fixture
    roles = WaitingRoles()
    service = ReportRunService(store, dependencies(roles))
    app.dependency_overrides[get_report_run_service] = lambda: service
    response = client.post("/api/v1/report-runs", json={
        "request_id": str(uuid4()), "session_id": session,
        "evidence_revision": 0, "accident_data": {"事实": "仅合成取消样本"},
    })
    assert response.status_code == 201
    return response.json()["run_id"], roles


@pytest.mark.parametrize("stop_mode", ["disconnect", "cancel", "delete"])
def test_real_sse_disconnect_cancel_or_delete_stops_worker(tool_client, stop_mode):
    client, store, owner, session = tool_client
    run_id, roles = waiting_run(tool_client)
    with loopback_app() as address:
        with httpx.Client(base_url=address, headers={
            "Authorization": client.headers["Authorization"],
        }, timeout=5, trust_env=False) as network:
            with network.stream(
                "POST", f"/api/v1/report-runs/{run_id}/execute/stream",
                json={"expected_version": 0},
            ) as response:
                assert response.status_code == 200
                lines = response.iter_lines()
                assert next(lines).startswith("data:")
                assert roles.entered.wait(5)
                if stop_mode == "cancel":
                    cancelled = client.post(f"/api/v1/report-runs/{run_id}/cancel")
                    assert cancelled.status_code == 200
                    assert cancelled.json()["state"] == "cancelled"
                    assert roles.closed.wait(5)
                elif stop_mode == "delete":
                    delete_session(
                        store.connection, session, owner_user_id=owner, owner_username=owner,
                    )
                    assert roles.closed.wait(5)
            assert roles.closed.wait(5)
    if stop_mode == "delete":
        assert client.get(f"/api/v1/report-runs/{run_id}").status_code == 404
        with store.connection() as conn:
            deleted = conn.execute(
                "SELECT state,deleted_at,document FROM report_runs WHERE run_id=%s", (run_id,),
            ).fetchone()
        assert deleted["state"] == "cancelled" and deleted["deleted_at"] is not None
        assert "report" not in deleted["document"]
        return
    record = store.get(owner, run_id)
    assert record["state"] == "cancelled"
    assert "report" not in record
    assert len([
        item for item in store.events(owner, run_id)["events"] if item["type"] == "final"
    ]) == 1


def test_database_serializes_cancel_and_publication(pg_store):
    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    store.transition(owner, run_id, token, "generating", {})
    store.transition(owner, run_id, token, "checking", {})
    barrier = threading.Barrier(2)

    def publish():
        barrier.wait(timeout=5)
        try:
            return store.transition(owner, run_id, token, "published", {
                "report": {"report_markdown": "合成事务测试正文"},
            }, "final", {"state": "published"})["state"]
        except HarnessError as exc:
            assert exc.code == "lease_lost"
            return "blocked"

    def cancel():
        barrier.wait(timeout=5)
        return store.cancel(owner, run_id)["state"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(publish)
        cancelling = pool.submit(cancel)
        published, cancelled = publishing.result(5), cancelling.result(5)
    record = store.get(owner, run_id)
    if record["state"] == "published":
        assert published == cancelled == "published"
        assert record["report"]["report_markdown"] == "合成事务测试正文"
    else:
        assert published == "blocked" and cancelled == "cancelled"
        assert "report" not in record
    assert len([
        item for item in store.events(owner, run_id)["events"]
        if item["data"].get("state") == "cancelled" or item["data"].get("state") == "published"
    ]) == 1


@pytest.mark.parametrize("first", ["published", "cancelled"])
def test_first_committed_terminal_state_cannot_be_reversed(pg_store, first):
    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    store.transition(owner, run_id, token, "generating", {})
    store.transition(owner, run_id, token, "checking", {})
    if first == "published":
        original = store.transition(
            owner, run_id, token, "published", {"report": {"report_markdown": "合成正文"}},
        )
        assert store.cancel(owner, run_id) == original
    else:
        original = store.cancel(owner, run_id)
        with pytest.raises(HarnessError, match="lease_lost"):
            store.transition(owner, run_id, token, "published", {"report": "禁止写入"})
    assert store.get(owner, run_id) == original


def test_cancel_and_late_settlement_preserve_terminal_document(pg_store):
    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    ledger = RequestLedger(store)
    request = reserve(ledger, owner, run_id, token)
    ledger.dispatch(owner, run_id, token, request["request_id"], request["attempt_id"])
    barrier = threading.Barrier(2)

    def settle():
        barrier.wait(timeout=5)
        ledger.settle(
            owner, run_id, token, request["request_id"], request["attempt_id"],
            actual_tokens=4, result={"synthetic": "迟到响应"},
        )

    def cancel():
        barrier.wait(timeout=5)
        return store.cancel(owner, run_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        settling = pool.submit(settle)
        cancelling = pool.submit(cancel)
        cancelled = cancelling.result(5)
        settling.result(5)
    record = store.get(owner, run_id)
    assert record["state"] == "cancelled"
    assert record["state_version"] == cancelled["state_version"]
    assert "report" not in record
    assert ledger.view(owner, run_id)["known_used"] == 4
    with pytest.raises(HarnessError, match="lease_lost"):
        reserve(ledger, owner, run_id, token)


def test_deleted_run_accepts_only_original_attempt_settlement(pg_store):
    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    _, _, other, session = pg_store
    ledger = RequestLedger(store)
    sent = reserve(ledger, owner, run_id, token)
    unsent = reserve(ledger, owner, run_id, token)
    ledger.dispatch(owner, run_id, token, sent["request_id"], sent["attempt_id"])
    delete_session(store.connection, session, owner_user_id=owner, owner_username=owner)
    with store.connection() as conn:
        before = conn.execute(
            "SELECT state,state_version,document FROM report_runs WHERE run_id=%s", (run_id,),
        ).fetchone()
    ledger.mark_unknown(owner, run_id, token, sent["request_id"], sent["attempt_id"])
    ledger.reject_unsent(owner, run_id, token, unsent["request_id"], unsent["attempt_id"])
    ledger.settle(
        owner, run_id, token, sent["request_id"], sent["attempt_id"],
        actual_tokens=4, result={"synthetic": "删除后迟到结算"},
    )
    with pytest.raises(HarnessError, match="not_found"):
        ledger.settle(
            other, run_id, token, sent["request_id"], sent["attempt_id"],
            actual_tokens=4, result={"synthetic": "删除后迟到结算"},
        )
    with store.connection() as conn:
        after = conn.execute(
            "SELECT state,state_version,document FROM report_runs WHERE run_id=%s", (run_id,),
        ).fetchone()
        settled = conn.execute(
            "SELECT status,actual_tokens FROM report_run_requests WHERE request_id=%s",
            (sent["request_id"],),
        ).fetchone()
    assert after == before
    assert settled == {"status": "committed", "actual_tokens": 4}
    with pytest.raises(HarnessError, match="not_found"):
        store.get(owner, run_id)


def test_legacy_database_without_harness_tables_keeps_delete_behavior(pg_store):
    store, owner, _, session = pg_store
    schema = "legacy_delete_" + uuid4().hex
    with store.connection() as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL(
            "CREATE TABLE {}.chat_sessions (id text PRIMARY KEY, "
            "owner_user_id uuid,owner_username text)",
        ).format(sql.Identifier(schema)))
        conn.execute(sql.SQL(
            "INSERT INTO {}.chat_sessions VALUES (%s,%s,%s)",
        ).format(sql.Identifier(schema)), (session, owner, owner))

    @contextmanager
    def legacy_connection():
        with store.connection() as conn:
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            yield conn

    try:
        result = delete_session(
            legacy_connection, session, owner_user_id=owner, owner_username=owner,
        )
        assert result["cancelled_run_ids"] == []
        with legacy_connection() as conn:
            assert conn.execute("SELECT count(*) AS total FROM chat_sessions").fetchone()["total"] == 0
        with store.connection() as conn:
            assert conn.execute(
                "SELECT id FROM chat_sessions WHERE id=%s", (session,),
            ).fetchone() is not None
    finally:
        with store.connection() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
