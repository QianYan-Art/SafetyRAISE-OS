from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from app.report_harness.errors import HarnessError
from app.report_harness.recovery import RunRecovery
from app.report_harness.store import RunStore
from app.schemas.report_run import BudgetPolicy


def _document(session_id: str, policy: BudgetPolicy | None = None) -> dict:
    return {
        "run_id": str(uuid4()),
        "session_id": session_id,
        "evidence_revision": 0,
        "parent_run_id": None,
        "endpoint_profile_digest": "e" * 64,
        "budget_policy": (policy or BudgetPolicy()).model_dump(mode="json"),
    }


def _create_run(pg_store, *, policy: BudgetPolicy | None = None):
    store, owner, _, session_id = pg_store
    document = _document(session_id, policy)
    run = store.create(owner, uuid4(), "q" * 64, document)
    return store, owner, session_id, run, document


def _set_run(store, run_id: str, *, state: str, token: int = 4, expired: bool = True,
             document: dict | None = None, version: int = 1, last_seq: int = 0):
    lease = "clock_timestamp()-interval '5 seconds'" if expired else "clock_timestamp()+interval '30 seconds'"
    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET state=%s,state_version=%s,last_event_seq=%s,"
            "fencing_token=%s,lease_owner=%s,lease_expires_at=" + lease + ",document=%s "
            "WHERE run_id=%s",
            (state, version, last_seq, token, uuid4(), Jsonb(document or {}), run_id),
        )


def _insert_request(store, run_id: str, *, status: str, reserved: int = 10,
                    actual: int | None = None, result: dict | None = None):
    request_id = uuid4()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO report_run_requests(request_id,run_id,attempt_id,fencing_token,"
            "role,endpoint_digest,request_digest,status,reserved_tokens,actual_tokens,result) "
            "VALUES (%s,%s,%s,4,'generator',%s,%s,%s,%s,%s,%s)",
            (request_id, run_id, uuid4(), "e" * 64, "r" * 64, status, reserved,
             actual, Jsonb(result) if result is not None else None),
        )
    return str(request_id)


def _run_row(store, run_id: str) -> dict:
    with store.connection() as conn:
        return conn.execute(
            "SELECT state,state_version,last_event_seq,fencing_token,lease_owner,"
            "lease_expires_at,document FROM report_runs WHERE run_id=%s",
            (run_id,),
        ).fetchone()


def test_recover_expired_caps_active_time_and_reconciles_requests(pg_store):
    store, owner, session_id, run, original_document = _create_run(pg_store)
    document = {
        **original_document,
        "active_seconds": 3.0,
        "active_started_at": "2020-01-01T00:00:00+00:00",
        "body": "保留",
    }
    _set_run(store, run["run_id"], state="generating", document=document)
    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET lease_expires_at=%s WHERE run_id=%s",
            (datetime(2020, 1, 1, 0, 0, 5, tzinfo=timezone.utc), run["run_id"]),
        )
    first = _insert_request(store, run["run_id"], status="intent")
    second = _insert_request(store, run["run_id"], status="dispatched")

    recovered = RunRecovery(store).recover_expired(owner, run["run_id"])
    row = _run_row(store, run["run_id"])

    assert recovered["state"] == "suspended"
    assert row["state"] == "suspended"
    assert row["state_version"] == 2
    assert row["last_event_seq"] == 1
    assert row["fencing_token"] == 5
    assert row["lease_owner"] is None
    assert row["lease_expires_at"] is None
    assert row["document"]["active_seconds"] == pytest.approx(8.0, abs=0.01)
    assert row["document"]["active_started_at"] is None
    assert row["document"]["terminal_reason"] == "orphaned_process"
    with store.connection() as conn:
        statuses = conn.execute(
            "SELECT request_id,status FROM report_run_requests WHERE run_id=%s",
            (run["run_id"],),
        ).fetchall()
        event = conn.execute(
            "SELECT type,state_version,data FROM report_run_events WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()
    assert {str(item["request_id"]): item["status"] for item in statuses} == {
        first: "completion_unknown", second: "completion_unknown",
    }
    assert event["type"] == "error"
    assert event["state_version"] == 2
    assert event["data"]["reason"] == "orphaned_process"


def test_recover_does_not_touch_queued_or_live_lease(pg_store):
    store, owner, session_id, queued, _ = _create_run(pg_store)
    before = _run_row(store, queued["run_id"])
    assert RunRecovery(store).recover_expired(owner, queued["run_id"])["state"] == "queued"
    assert _run_row(store, queued["run_id"])["state_version"] == before["state_version"]

    _set_run(store, queued["run_id"], state="generating", expired=False,
             document=_document(session_id))
    assert RunRecovery(store).recover_expired(owner, queued["run_id"])["state"] == "generating"
    assert _run_row(store, queued["run_id"])["state_version"] == 1


def test_background_sweep_reconciles_without_user_get_or_resume(pg_store):
    store, owner, session_id, run, document = _create_run(pg_store)
    _set_run(store, run["run_id"], state="generating", document=document)
    request_id = _insert_request(store, run["run_id"], status="dispatched")
    recovery = RunRecovery(store)

    recovery.sweep_expired()
    row = _run_row(store, run["run_id"])
    assert row["state"] == "suspended"
    assert row["document"]["terminal_reason"] == "orphaned_process"
    with store.connection() as conn:
        request = conn.execute(
            "SELECT status FROM report_run_requests WHERE request_id=%s", (request_id,),
        ).fetchone()
    assert request["status"] == "completion_unknown"
    recovery.sweep_expired()
    assert _run_row(store, run["run_id"]) == row


def test_background_sweep_preserves_live_and_cancelled_runs(pg_store):
    store, owner, session_id, run, document = _create_run(pg_store)
    _set_run(store, run["run_id"], state="generating", expired=False, document=document)
    before = _run_row(store, run["run_id"])
    RunRecovery(store).sweep_expired()
    assert _run_row(store, run["run_id"]) == before
    _set_run(store, run["run_id"], state="cancelled", document=document)
    before = _run_row(store, run["run_id"])
    RunRecovery(store).sweep_expired()
    assert _run_row(store, run["run_id"]) == before


def test_resume_default_unknown_rejects_without_mutation(pg_store):
    policy = BudgetPolicy(max_physical_requests=5, max_retrieval_requests=5,
                          max_total_tokens=100)
    store, owner, session_id, run, document = _create_run(pg_store, policy=policy)
    suspended_document = {**document, "terminal_reason": "orphaned_process"}
    _set_run(store, run["run_id"], state="suspended", token=7, document=suspended_document,
             version=2, last_seq=1)
    unknown_id = _insert_request(store, run["run_id"], status="completion_unknown", reserved=30)
    committed_id = _insert_request(
        store, run["run_id"], status="committed", reserved=20, result={"ok": True},
    )
    before = _run_row(store, run["run_id"])

    with pytest.raises(HarnessError) as exc:
        RunRecovery(store).resume_claim(
            owner, run["run_id"], 2, uuid4(), retry_unknown_requests=False,
            validate=lambda value: None, minimum_requests=1, minimum_tokens=10,
        )
    assert exc.value.code == "completion_unknown"
    assert exc.value.details["unknown_request_ids"] == sorted([unknown_id, committed_id])
    after = _run_row(store, run["run_id"])
    assert after == before


def test_resume_accepts_unknown_and_retains_reserves(pg_store):
    policy = BudgetPolicy(max_physical_requests=5, max_retrieval_requests=5,
                          max_total_tokens=100)
    store, owner, _, run, document = _create_run(pg_store, policy=policy)
    _set_run(store, run["run_id"], state="suspended", token=7, document=document,
             version=2, last_seq=1)
    unknown_id = _insert_request(store, run["run_id"], status="completion_unknown", reserved=30)
    committed_id = _insert_request(
        store, run["run_id"], status="committed", reserved=20, result={"ok": True},
    )
    worker = uuid4()

    token = RunRecovery(store).resume_claim(
        owner, run["run_id"], 2, worker, retry_unknown_requests=True,
        validate=lambda value: value.__setitem__("ignored", True),
        minimum_requests=1, minimum_tokens=20,
    )
    row = _run_row(store, run["run_id"])
    assert token == 8
    assert row["state"] == "preparing"
    assert row["state_version"] == 3
    assert row["fencing_token"] == 8
    assert str(row["lease_owner"]) == str(worker)
    assert row["lease_expires_at"] is not None
    assert row["document"]["approved_unknown_request_ids"] == sorted([unknown_id, committed_id])
    assert row["document"]["active_started_at"] is not None
    assert row["document"]["terminal_reason"] is None
    with store.connection() as conn:
        event = conn.execute(
            "SELECT type,data FROM report_run_events WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()
        aggregate = conn.execute(
            "SELECT COALESCE(SUM(reserved_tokens) FILTER (WHERE status='completion_unknown' "
            "OR (status='committed' AND actual_tokens IS NULL)),0) AS unknown_reserved "
            "FROM report_run_requests WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()
    assert event["type"] == "checkpoint"
    assert event["data"]["approved_unknown_request_ids"] == sorted([unknown_id, committed_id])
    assert aggregate["unknown_reserved"] == 50


def test_resume_budget_and_money_rejection_leave_run_unchanged(pg_store):
    policy = BudgetPolicy(max_physical_requests=5, max_retrieval_requests=5,
                          max_total_tokens=40)
    store, owner, _, run, document = _create_run(pg_store, policy=policy)
    _set_run(store, run["run_id"], state="suspended", token=7, document=document,
             version=2, last_seq=1)
    _insert_request(store, run["run_id"], status="completion_unknown", reserved=30)
    before = _run_row(store, run["run_id"])
    with pytest.raises(HarnessError, match="token_budget_exhausted"):
        RunRecovery(store).resume_claim(
            owner, run["run_id"], 2, uuid4(), retry_unknown_requests=True,
            validate=lambda value: None, minimum_requests=1, minimum_tokens=20,
        )
    assert _run_row(store, run["run_id"]) == before

    money_policy = BudgetPolicy(max_physical_requests=5, max_retrieval_requests=5,
                                max_total_tokens=100, max_money=Decimal("1"))
    money_document = {**document, "budget_policy": money_policy.model_dump(mode="json")}
    _set_run(store, run["run_id"], state="suspended", token=1,
             document=money_document, version=1, last_seq=0)
    before_money = _run_row(store, run["run_id"])
    with pytest.raises(HarnessError, match="money_budget_unverifiable"):
        RunRecovery(store).resume_claim(
            owner, run["run_id"], 1, uuid4(), retry_unknown_requests=True,
            validate=lambda value: None, minimum_requests=0, minimum_tokens=0,
        )
    assert _run_row(store, run["run_id"]) == before_money


def test_resume_races_are_serialized_by_run_lock(pg_store):
    store, owner, _, run, document = _create_run(pg_store)
    _set_run(store, run["run_id"], state="suspended", token=2, document=document,
             version=1, last_seq=0)
    recovery = RunRecovery(store)

    def attempt():
        try:
            return ("ok", recovery.resume_claim(
                owner, run["run_id"], 1, uuid4(), retry_unknown_requests=False,
                validate=lambda value: None, minimum_requests=0, minimum_tokens=0,
            ))
        except HarnessError as exc:
            return ("error", exc.code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: attempt(), range(2)))
    assert [item[0] for item in results].count("ok") == 1
    assert [item[1] for item in results].count("version_conflict") == 1
    assert _run_row(store, run["run_id"])["state"] == "preparing"


def test_recovery_rejects_cancelled_or_deleted_run_and_old_token(pg_store):
    store, owner, _, run, document = _create_run(pg_store)
    _set_run(store, run["run_id"], state="generating", token=5, document=document)
    store.cancel(owner, run["run_id"], expected_token=5)
    assert RunRecovery(store).recover_expired(owner, run["run_id"])["state"] == "cancelled"
    with pytest.raises(HarnessError, match="lease_lost"):
        store.assert_active(owner, run["run_id"], 5)

    deleted_run = store.create(owner, uuid4(), "q" * 64, _document(run["session_id"]))
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO session_deletion_barriers(session_id) VALUES (%s) "
            "ON CONFLICT DO NOTHING",
            (run["session_id"],),
        )
    try:
        with pytest.raises(HarnessError, match="not_found"):
            RunRecovery(store).recover_expired(owner, deleted_run["run_id"])
    finally:
        with store.connection() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM session_deletion_barriers WHERE session_id=%s",
                (run["session_id"],),
            )


def test_resume_validation_failure_does_not_mutate(pg_store):
    store, owner, _, run, document = _create_run(pg_store)
    _set_run(store, run["run_id"], state="suspended", token=2, document=document,
             version=1, last_seq=0)
    before = _run_row(store, run["run_id"])

    def invalid(_document):
        raise ValueError("版本不一致")

    with pytest.raises(ValueError, match="版本不一致"):
        RunRecovery(store).resume_claim(
            owner, run["run_id"], 1, uuid4(), retry_unknown_requests=False,
            validate=invalid, minimum_requests=0, minimum_tokens=0,
        )
    assert _run_row(store, run["run_id"]) == before
