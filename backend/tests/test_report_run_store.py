from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event
from time import monotonic, sleep
from uuid import uuid4

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.store import RunStore
from app.report_harness.test_database import migrate_test_database


def document(session):
    return {
        "run_id": str(uuid4()), "session_id": session,
        "evidence_revision": 0, "parent_run_id": None,
    }


def test_create_is_idempotent_and_owner_scoped(pg_store):
    store, owner, other, session = pg_store
    store.check_schema()
    request_id = uuid4()
    first = store.create(owner, request_id, "digest-1", document(session))
    again = store.create(owner, request_id, "digest-1", document(session))
    assert first["run_id"] == again["run_id"]
    with pytest.raises(HarnessError, match="idempotency_conflict"):
        store.create(owner, request_id, "digest-2", document(session))
    with pytest.raises(HarnessError) as exc:
        store.get(other, first["run_id"])
    assert exc.value.status_code == 404
    assert RunStore(store.connection).get(owner, first["run_id"]) == first


def test_applied_migrations_do_not_take_table_ddl_locks(pg_store):
    store, _, _, _ = pg_store
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.connection() as holder:
            holder.execute("LOCK TABLE report_runs IN ACCESS SHARE MODE")
            migration = pool.submit(
                migrate_test_database, os.environ["REPORT_HARNESS_TEST_DSN"],
            )
            migration.result(timeout=3)


def test_session_active_uniqueness(pg_store):
    store, owner, _, session = pg_store
    store.create(owner, uuid4(), "first", document(session))
    with pytest.raises(HarnessError, match="active_run_conflict"):
        store.create(owner, uuid4(), "second", document(session))


def test_historical_username_owned_session_remains_accessible(pg_store):
    store, owner, other, session = pg_store
    with store.connection() as conn:
        conn.execute(
            "UPDATE chat_sessions SET owner_user_id=NULL,owner_username=%s WHERE id=%s",
            (owner, session),
        )
    created = store.create(owner, uuid4(), "legacy", document(session))
    assert store.get(owner, created["run_id"])["session_id"] == session
    with pytest.raises(HarnessError) as exc:
        store.create(other, uuid4(), "other", document(session))
    assert exc.value.status_code == 404


def test_two_connections_cannot_both_acquire(pg_store):
    store, owner, _, session = pg_store
    created = store.create(owner, uuid4(), "digest", document(session))

    def acquire():
        try:
            return RunStore(store.connection).acquire(owner, created["run_id"], 0, uuid4())
        except HarnessError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: acquire(), range(2)))
    assert sum(isinstance(item, int) for item in results) == 1
    assert store.get(owner, created["run_id"])["state"] == "preparing"


def test_cancel_fences_old_worker_and_preserves_events(pg_store):
    store, owner, _, session = pg_store
    created = store.create(owner, uuid4(), "digest", document(session))
    run_id = created["run_id"]
    token = store.acquire(owner, run_id, 0, uuid4())
    cancelled = store.cancel(owner, run_id)
    with pytest.raises(HarnessError, match="lease_lost"):
        store.transition(owner, run_id, token, "published", {"report": "不应发布"})
    assert store.cancel(owner, run_id) == cancelled
    assert store.get(owner, run_id)["state"] == "cancelled"
    assert [event["seq"] for event in store.events(owner, run_id)["events"]] == [1, 2]


def test_event_and_state_transaction_roll_back_together(pg_store):
    store, owner, _, session = pg_store
    created = store.create(owner, uuid4(), "digest", document(session))
    with pytest.raises(RuntimeError):
        with store.locked(owner, created["run_id"]) as (conn, row):
            store.save(conn, row, "preparing", row["document"], "stage", {})
            raise RuntimeError("注入事务故障")
    assert store.get(owner, created["run_id"])["state"] == "queued"
    assert store.events(owner, created["run_id"])["events"] == []


def test_old_worker_cannot_cancel_new_lease(pg_store):
    store, owner, _, session = pg_store
    run = store.create(owner, uuid4(), "digest", document(session))
    token = store.acquire(owner, run["run_id"], 0, uuid4())
    with store.connection() as conn:
        conn.execute("UPDATE report_runs SET fencing_token=fencing_token+1 WHERE run_id=%s",
                     (run["run_id"],))
    assert store.cancel(owner, run["run_id"], expected_token=token)["state"] == "preparing"


def test_concurrent_request_key_across_sessions_has_precise_conflict(pg_store):
    from threading import Barrier

    store, owner, _, first_session = pg_store
    second_session = "harness-" + str(uuid4())
    with store.connection() as conn:
        conn.execute("INSERT INTO chat_sessions(id,owner_user_id) VALUES (%s,%s)",
                     (second_session, owner))
    request_id = uuid4()
    barrier = Barrier(2)

    def create_in_session(session):
        barrier.wait(timeout=5)
        try:
            return store.create(owner, request_id, session, document(session))["run_id"]
        except HarnessError as exc:
            return exc.code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create_in_session, [first_session, second_session]))
        assert results.count("idempotency_conflict") == 1
        assert "active_run_conflict" not in results
    finally:
        with store.connection() as conn:
            conn.execute("DELETE FROM report_runs WHERE session_id=%s", (second_session,))
            conn.execute("DELETE FROM chat_sessions WHERE id=%s", (second_session,))


def test_lease_expiring_while_waiting_for_row_lock_is_rejected(pg_store):
    store, owner, _, session = pg_store
    run = store.create(owner, uuid4(), "lock-expiry", document(session))
    run_id = run["run_id"]
    token = store.acquire(owner, run_id, 0, uuid4())
    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET lease_expires_at=clock_timestamp()+interval '1 second' "
            "WHERE run_id=%s", (run_id,),
        )
    started = Event()
    waiter_pid = []

    # 确认第二个连接确实进入锁等待，再让租约过期；不能只凭线程启动推定发生竞争。
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.connection() as holder:
            holder.execute("SELECT run_id FROM report_runs WHERE run_id=%s FOR UPDATE", (run_id,))
            def read_locked():
                with store.connection() as conn:
                    waiter_pid.append(conn.execute("SELECT pg_backend_pid()").fetchone()["pg_backend_pid"])
                    started.set()
                    return store._read(conn, owner, run_id, lock=True)
            future = pool.submit(read_locked)
            assert started.wait(5)
            deadline = monotonic() + 5
            while True:
                with store.connection() as observer:
                    waiting = observer.execute(
                        "SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                        (waiter_pid[0],),
                    ).fetchone()
                if waiting and waiting["wait_event_type"] == "Lock":
                    break
                assert monotonic() < deadline, "未观察到真实行锁竞争"
                sleep(0.01)
            holder.execute("SELECT pg_sleep(1.1)")
        row = future.result(timeout=5)
    assert row["db_now"] >= row["lease_expires_at"]
    with pytest.raises(HarnessError, match="lease_lost"):
        store.assert_active(owner, run_id, token)


def test_suspension_freezes_accumulated_active_time(pg_store):
    store, owner, _, session = pg_store
    run = store.create(owner, uuid4(), "active-time", document(session))
    run_id = run["run_id"]
    token = store.acquire(owner, run_id, 0, uuid4())
    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET document=jsonb_set(document,'{active_started_at}',"
            "to_jsonb((clock_timestamp()-interval '2 seconds')::text)) WHERE run_id=%s",
            (run_id,),
        )
    before = store.get(owner, run_id)
    assert before["active_seconds"] >= 2
    paused = store.transition(owner, run_id, token, "suspended", before)
    assert paused["active_seconds"] >= before["active_seconds"]
    with store.connection() as conn:
        conn.execute("SELECT pg_sleep(0.05)")
    assert store.get(owner, run_id)["active_seconds"] == paused["active_seconds"]
