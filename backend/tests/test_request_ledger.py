from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.request_ledger import RequestLedger
from app.schemas.report_run import BudgetPolicy


ENDPOINT_DIGEST = "e" * 64
REQUEST_DIGEST = "a" * 64


def create_run(pg_store, *, policy: BudgetPolicy | None = None):
    store, owner, _, session = pg_store
    run = store.create(
        owner,
        uuid4(),
        "request-" + str(uuid4()),
        {
            "run_id": str(uuid4()),
            "session_id": session,
            "evidence_revision": 0,
            "parent_run_id": None,
            "endpoint_profile_digest": ENDPOINT_DIGEST,
        },
    )
    document_patch = {"endpoint_profile_digest": ENDPOINT_DIGEST}
    if policy is not None:
        document_patch["budget_policy"] = policy.model_dump(mode="json")
    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET document=document || %s WHERE run_id=%s",
            (Jsonb(document_patch), run["run_id"]),
        )
    token = store.acquire(owner, run["run_id"], 0, uuid4())
    return store, owner, run["run_id"], token


def reserve(
    ledger,
    owner,
    run_id,
    token,
    *,
    role="generator",
    amount=10,
    review=5,
    generation=0,
):
    return ledger.reserve(
        owner,
        run_id,
        token,
        role=role,
        endpoint_digest=ENDPOINT_DIGEST,
        request_digest=REQUEST_DIGEST,
        reserved_tokens=amount,
        review_reserve_tokens=review,
        generation_reserve_tokens=generation,
    )


@pytest.mark.parametrize("actual,expected", [(None, "usage_unknown"), (11, "usage_exceeded")])
def test_dispatch_rechecks_late_settlement_after_reservation(pg_store, actual, expected):
    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    ledger = RequestLedger(store)
    first = reserve(ledger, owner, run_id, token)
    pending = reserve(ledger, owner, run_id, token)
    ledger.dispatch(owner, run_id, token, first["request_id"], first["attempt_id"])
    ledger.settle(
        owner, run_id, token, first["request_id"], first["attempt_id"],
        actual_tokens=actual, result={"synthetic": True},
    )
    with pytest.raises(HarnessError, match=expected):
        ledger.dispatch(owner, run_id, token, pending["request_id"], pending["attempt_id"])
    with store.connection() as conn:
        row = conn.execute(
            "SELECT status FROM report_run_requests WHERE request_id=%s",
            (pending["request_id"],),
        ).fetchone()
    assert row["status"] == "intent"


def test_late_settlement_cannot_mix_request_from_another_run(pg_store):
    store, owner, first_run, token = create_run(pg_store, policy=BudgetPolicy())
    ledger = RequestLedger(store)
    request = reserve(ledger, owner, first_run, token)
    ledger.dispatch(owner, first_run, token, request["request_id"], request["attempt_id"])
    store.cancel(owner, first_run)
    _, _, second_run, _ = create_run(pg_store, policy=BudgetPolicy())
    with pytest.raises(HarnessError, match="request_not_found"):
        ledger.settle(
            owner, second_run, token, request["request_id"], request["attempt_id"],
            actual_tokens=2, result={"synthetic": True},
        )
    assert ledger.view(owner, second_run)["physical_requests"] == 0
    assert ledger.view(owner, first_run)["known_used"] == 0


def test_missing_policy_and_request_input_validation(pg_store):
    store, owner, run_id, token = create_run(pg_store)
    ledger = RequestLedger(store)

    with pytest.raises(HarnessError) as error:
        reserve(ledger, owner, run_id, token)
    assert error.value.code == "budget_policy_missing"

    with pytest.raises(HarnessError) as error:
        ledger.reserve(
            owner,
            run_id,
            token,
            role="unknown",
            endpoint_digest=ENDPOINT_DIGEST,
            request_digest=REQUEST_DIGEST,
            reserved_tokens=1,
            review_reserve_tokens=0,
        )
    assert error.value.code == "invalid_request_role"

    with pytest.raises(HarnessError) as error:
        ledger.reserve(
            owner,
            run_id,
            token,
            role="generator",
            endpoint_digest=ENDPOINT_DIGEST,
            request_digest="R" * 64,
            reserved_tokens=1,
            review_reserve_tokens=0,
        )
    assert error.value.code == "invalid_request_digest"

    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET document=document || %s WHERE run_id=%s",
            (Jsonb({"budget_policy": BudgetPolicy().model_dump(mode="json")}), run_id),
        )
    with pytest.raises(HarnessError) as error:
        ledger.reserve(
            owner,
            run_id,
            token,
            role="generator",
            endpoint_digest="wrong",
            request_digest=REQUEST_DIGEST,
            reserved_tokens=1,
            review_reserve_tokens=0,
        )
    assert error.value.code == "endpoint_digest_conflict"


def test_view_reads_money_policy_but_execution_paths_reject_it(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(
            max_physical_requests=5,
            max_retrieval_requests=5,
            max_total_tokens=100,
            max_money=1,
        ),
    )
    ledger = RequestLedger(store)

    assert ledger.view(owner, run_id)["remaining"] == 100
    with pytest.raises(HarnessError) as error:
        reserve(ledger, owner, run_id, token, amount=1, review=0)
    assert error.value.code == "money_budget_unverifiable"


def test_reserve_dispatch_events_preserve_run_document_and_settle_does_not_save(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(max_physical_requests=4, max_retrieval_requests=4, max_total_tokens=100),
    )
    ledger = RequestLedger(store)
    before = store.get(owner, run_id)

    request = reserve(ledger, owner, run_id, token, amount=20, review=10)
    assert request["status"] == "intent"
    assert request["reserved_tokens"] == 20
    assert request["actual_tokens"] is None
    dispatched = ledger.dispatch(owner, run_id, token, request["request_id"], request["attempt_id"])
    assert dispatched["status"] == "dispatched"
    settled = ledger.settle(
        owner,
        run_id,
        token,
        request["request_id"],
        request["attempt_id"],
        actual_tokens=7,
        result={"ok": True},
    )
    assert settled["status"] == "committed"
    assert settled["actual_tokens"] == 7
    assert settled["result"] == {"ok": True}

    after = store.get(owner, run_id)
    assert after["state"] == before["state"] == "preparing"
    assert after["state_version"] == before["state_version"] + 2
    assert after["last_event_seq"] == before["last_event_seq"] + 2
    for key in (
        "run_id",
        "session_id",
        "evidence_revision",
        "parent_run_id",
        "endpoint_profile_digest",
        "budget_policy",
    ):
        assert after[key] == before[key]
    request_events = [
        event
        for event in store.events(owner, run_id)["events"]
        if event["type"] == "request"
    ]
    assert [event["data"]["status"] for event in request_events] == [
        "intent",
        "dispatched",
    ]
    assert request_events[0]["data"] == {
        "request_id": request["request_id"],
        "attempt_id": request["attempt_id"],
        "role": "generator",
        "status": "intent",
        "reserved_tokens": 20,
    }
    assert request_events[1]["data"] == {
        "request_id": request["request_id"],
        "attempt_id": request["attempt_id"],
        "role": "generator",
        "status": "dispatched",
        "reserved_tokens": 20,
    }
    assert ledger.view(owner, run_id) == {
        "physical_requests": 1,
        "known_used": 7,
        "unknown_reserved": 0,
        "inflight_reserved": 0,
        "remaining": 93,
        "unknown_requests": 0,
        "usage_exceeded": False,
        "retrieval_requests": 0,
    }


def test_ensure_capacity_and_preparing_generation_reserve(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(
            max_physical_requests=3,
            max_retrieval_requests=3,
            max_total_tokens=60,
        ),
    )
    ledger = RequestLedger(store)

    ledger.ensure_capacity(owner, run_id, token, requests=3, tokens=60)
    with pytest.raises(HarnessError) as error:
        ledger.ensure_capacity(owner, run_id, token, requests=4, tokens=0)
    assert error.value.code == "physical_request_budget_exhausted"
    with pytest.raises(HarnessError) as error:
        ledger.ensure_capacity(owner, run_id, token, requests=0, tokens=61)
    assert error.value.code == "token_budget_exhausted"

    expert = reserve(
        ledger,
        owner,
        run_id,
        token,
        role="expert",
        amount=10,
        review=20,
        generation=30,
    )
    assert expert["status"] == "intent"

    with pytest.raises(HarnessError) as error:
        reserve(
            ledger,
            owner,
            run_id,
            token,
            role="expert",
            amount=10,
            review=20,
            generation=30,
        )
    assert error.value.code == "physical_request_budget_exhausted"


def test_concurrent_reservations_serialize_against_run_lock(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(max_physical_requests=4, max_retrieval_requests=4, max_total_tokens=25),
    )
    ledger = RequestLedger(store)

    def attempt(_):
        try:
            return reserve(ledger, owner, run_id, token, amount=10, review=0)
        except HarnessError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(isinstance(item, dict) for item in results) == 2

    with pytest.raises(HarnessError) as error:
        reserve(ledger, owner, run_id, token, amount=6, review=0)
    assert error.value.code == "token_budget_exhausted"
    assert ledger.view(owner, run_id)["inflight_reserved"] == 20


def test_non_reviewer_reserves_final_review_capacity_and_reviewer_has_no_extra_slot(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(max_physical_requests=3, max_retrieval_requests=3, max_total_tokens=100),
    )
    ledger = RequestLedger(store)
    first = reserve(ledger, owner, run_id, token, role="generator", amount=60, review=30)

    with pytest.raises(HarnessError) as error:
        reserve(ledger, owner, run_id, token, role="generator", amount=11, review=30)
    assert error.value.code == "token_budget_exhausted"

    ledger.dispatch(owner, run_id, token, first["request_id"], first["attempt_id"])
    ledger.settle(
        owner,
        run_id,
        token,
        first["request_id"],
        first["attempt_id"],
        actual_tokens=60,
        result={"done": True},
    )
    reviewer = reserve(ledger, owner, run_id, token, role="reviewer", amount=40, review=999)
    assert reviewer["status"] == "intent"
    assert ledger.view(owner, run_id)["physical_requests"] == 1


def test_unknown_usage_blocks_future_reserve_and_view_counts_it_once(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(max_physical_requests=5, max_retrieval_requests=5, max_total_tokens=100),
    )
    ledger = RequestLedger(store)
    request = reserve(ledger, owner, run_id, token, amount=25, review=0)
    ledger.dispatch(owner, run_id, token, request["request_id"], request["attempt_id"])
    ledger.settle(
        owner,
        run_id,
        token,
        request["request_id"],
        request["attempt_id"],
        actual_tokens=None,
        result={"partial": True},
    )
    assert ledger.view(owner, run_id)["unknown_reserved"] == 25
    assert ledger.view(owner, run_id)["unknown_requests"] == 1
    with pytest.raises(HarnessError) as error:
        reserve(ledger, owner, run_id, token, amount=1, review=0)
    assert error.value.code == "usage_unknown"


def test_overage_is_persisted_and_blocks_future_reserve(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(max_physical_requests=5, max_retrieval_requests=5, max_total_tokens=100),
    )
    ledger = RequestLedger(store)
    request = reserve(ledger, owner, run_id, token, amount=5, review=0)
    ledger.dispatch(owner, run_id, token, request["request_id"], request["attempt_id"])
    settled = ledger.settle(
        owner,
        run_id,
        token,
        request["request_id"],
        request["attempt_id"],
        actual_tokens=8,
        result={"over": True},
    )
    assert settled["actual_tokens"] == 8
    view = ledger.view(owner, run_id)
    assert view["known_used"] == 8
    assert view["usage_exceeded"] is True
    with pytest.raises(HarnessError) as error:
        reserve(ledger, owner, run_id, token, amount=1, review=0)
    assert error.value.code == "usage_exceeded"


def test_settle_idempotency_allows_unknown_then_known_usage_but_rejects_conflicts(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(max_physical_requests=5, max_retrieval_requests=5, max_total_tokens=100),
    )
    ledger = RequestLedger(store)
    request = reserve(ledger, owner, run_id, token, amount=20, review=0)
    ledger.dispatch(owner, run_id, token, request["request_id"], request["attempt_id"])
    first = ledger.settle(
        owner,
        run_id,
        token,
        request["request_id"],
        request["attempt_id"],
        actual_tokens=None,
        result={"same": 1},
    )
    repeat_unknown = ledger.settle(
        owner,
        run_id,
        token,
        request["request_id"],
        request["attempt_id"],
        actual_tokens=None,
        result={"same": 1},
    )
    assert repeat_unknown == first
    known = ledger.settle(
        owner,
        run_id,
        token,
        request["request_id"],
        request["attempt_id"],
        actual_tokens=12,
        result={"same": 1},
    )
    assert known["actual_tokens"] == 12
    assert ledger.settle(
        owner,
        run_id,
        token,
        request["request_id"],
        request["attempt_id"],
        actual_tokens=12,
        result={"same": 1},
    ) == known
    before_conflicts = store.get(owner, run_id)
    with pytest.raises(HarnessError) as error:
        ledger.settle(
            owner,
            run_id,
            token,
            request["request_id"],
            request["attempt_id"],
            actual_tokens=13,
            result={"same": 1},
        )
    assert error.value.code == "settlement_conflict"
    assert ledger.view(owner, run_id)["known_used"] == 12
    with pytest.raises(HarnessError) as error:
        ledger.settle(
            owner,
            run_id,
            token,
            request["request_id"],
            request["attempt_id"],
            actual_tokens=12,
            result={"different": True},
        )
    assert error.value.code == "settlement_conflict"

    after_conflicts = store.get(owner, run_id)
    assert after_conflicts["state"] == before_conflicts["state"]
    assert after_conflicts["state_version"] == before_conflicts["state_version"]
    assert after_conflicts["budget_policy"] == before_conflicts["budget_policy"]
    assert after_conflicts["last_event_seq"] == before_conflicts["last_event_seq"] + 2

    audit_events = [
        event
        for event in store.events(owner, run_id)["events"]
        if event["type"] == "error"
        and event["data"].get("code") == "settlement_conflict"
    ]
    assert [event["data"]["reason"] for event in audit_events] == [
        "actual_tokens_mismatch",
        "result_mismatch",
    ]
    for event in audit_events:
        assert event["state_version"] == before_conflicts["state_version"]
        assert set(event["data"]) == {
            "code",
            "reason",
            "request_id",
            "attempt_id",
            "role",
            "status",
            "reserved_tokens",
            "stored_actual_tokens",
            "submitted_actual_tokens",
            "stored_result_digest",
            "submitted_result_digest",
        }
        assert event["data"]["request_id"] == request["request_id"]
        assert event["data"]["attempt_id"] == request["attempt_id"]
        assert event["data"]["stored_result_digest"] == canonical_digest({"same": 1})
        assert len(event["data"]["submitted_result_digest"]) == 64
        assert "result" not in event["data"]
        assert "response" not in event["data"]


def test_late_settlement_with_original_token_after_cancel_does_not_change_run(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(max_physical_requests=5, max_retrieval_requests=5, max_total_tokens=100),
    )
    ledger = RequestLedger(store)
    request = reserve(ledger, owner, run_id, token, amount=20, review=0)
    ledger.dispatch(owner, run_id, token, request["request_id"], request["attempt_id"])
    before_cancel = store.get(owner, run_id)
    store.cancel(owner, run_id, expected_token=token)
    cancelled = store.get(owner, run_id)
    settled = ledger.settle(
        owner,
        run_id,
        token,
        request["request_id"],
        request["attempt_id"],
        actual_tokens=9,
        result={"late": True},
    )
    after = store.get(owner, run_id)
    assert settled["status"] == "committed"
    assert after == cancelled
    assert before_cancel["state_version"] < after["state_version"]


def test_reject_unsent_releases_reservation_but_sent_request_cannot_be_rejected(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(max_physical_requests=5, max_retrieval_requests=5, max_total_tokens=100),
    )
    ledger = RequestLedger(store)
    unsent = reserve(ledger, owner, run_id, token, amount=30, review=0)
    assert ledger.view(owner, run_id)["inflight_reserved"] == 30
    assert ledger.reject_unsent(
        owner, run_id, token, unsent["request_id"], unsent["attempt_id"],
    ) is None
    assert ledger.view(owner, run_id)["inflight_reserved"] == 0

    sent = reserve(ledger, owner, run_id, token, amount=10, review=0)
    ledger.dispatch(owner, run_id, token, sent["request_id"], sent["attempt_id"])
    with pytest.raises(HarnessError) as error:
        ledger.reject_unsent(
            owner, run_id, token, sent["request_id"], sent["attempt_id"],
        )
    assert error.value.code == "request_state_conflict"


def test_confirmed_not_sent_rejects_dispatched_reservation_only(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(max_physical_requests=5, max_retrieval_requests=5, max_total_tokens=100),
    )
    ledger = RequestLedger(store)
    unsent = reserve(ledger, owner, run_id, token, amount=10, review=0)
    with pytest.raises(HarnessError, match="request_state_conflict"):
        ledger.reject_confirmed_not_sent(
            owner, run_id, token, unsent["request_id"], unsent["attempt_id"],
        )

    sent = reserve(ledger, owner, run_id, token, amount=30, review=0)
    ledger.dispatch(owner, run_id, token, sent["request_id"], sent["attempt_id"])
    ledger.reject_confirmed_not_sent(
        owner, run_id, token, sent["request_id"], sent["attempt_id"],
    )
    ledger.reject_confirmed_not_sent(
        owner, run_id, token, sent["request_id"], sent["attempt_id"],
    )
    view = ledger.view(owner, run_id)
    assert view["inflight_reserved"] == 10
    assert view["unknown_reserved"] == 0
    with pytest.raises(HarnessError, match="request_state_conflict"):
        ledger.mark_unknown(
            owner, run_id, token, sent["request_id"], sent["attempt_id"],
        )


def test_cancelled_run_allows_original_token_to_clean_intent(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(max_physical_requests=5, max_retrieval_requests=5, max_total_tokens=100),
    )
    ledger = RequestLedger(store)
    intent = reserve(ledger, owner, run_id, token, amount=20, review=0)
    store.cancel(owner, run_id, expected_token=token)
    cancelled = store.get(owner, run_id)

    ledger.reject_unsent(owner, run_id, token, intent["request_id"], intent["attempt_id"])
    after_reject = store.get(owner, run_id)
    assert after_reject == cancelled
    assert ledger.view(owner, run_id)["inflight_reserved"] == 0


def test_cancelled_run_allows_original_token_to_mark_unknown(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(
            max_physical_requests=5,
            max_retrieval_requests=5,
            max_total_tokens=100,
        ),
    )
    ledger = RequestLedger(store)
    dispatched = reserve(ledger, owner, run_id, token, amount=15, review=0)
    ledger.dispatch(owner, run_id, token, dispatched["request_id"], dispatched["attempt_id"])
    store.cancel(owner, run_id, expected_token=token)
    cancelled = store.get(owner, run_id)
    ledger.mark_unknown(owner, run_id, token, dispatched["request_id"], dispatched["attempt_id"])
    after_unknown = store.get(owner, run_id)
    assert after_unknown == cancelled
    assert ledger.view(owner, run_id)["unknown_reserved"] == 15


def test_mark_unknown_and_retrieval_sub_limit_are_accounted_separately(pg_store):
    store, owner, run_id, token = create_run(
        pg_store,
        policy=BudgetPolicy(max_physical_requests=5, max_retrieval_requests=1, max_total_tokens=100),
    )
    ledger = RequestLedger(store)
    first = reserve(ledger, owner, run_id, token, role="embedding", amount=5, review=0)
    ledger.dispatch(owner, run_id, token, first["request_id"], first["attempt_id"])
    ledger.mark_unknown(owner, run_id, token, first["request_id"], first["attempt_id"])
    view = ledger.view(owner, run_id)
    assert view["physical_requests"] == 1
    assert view["retrieval_requests"] == 1
    with pytest.raises(HarnessError) as error:
        reserve(ledger, owner, run_id, token, role="embedding", amount=1, review=0)
    assert error.value.code == "usage_unknown"
