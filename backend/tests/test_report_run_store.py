from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.store import RunStore


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
