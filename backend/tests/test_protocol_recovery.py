from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.journal import JOURNAL_VERSION
from app.report_harness.recovery import RunRecovery, can_resume_protocol
from app.services.report_run_service import ReportRunService
from app.schemas.report_run import CreateRunRequest
from tests.harness_fixtures import SyntheticRoles, dependencies


def _model_result(call_id: str = "protocol-call") -> dict:
    return {
        "call_id": call_id,
        "name": "list_evidence",
        "arguments": {},
    }


def _journal(model_results: list[dict] | None = None) -> dict:
    results = model_results or [_model_result()]
    entries = {}

    prepare_identity = {"snapshot": "snapshot", "contract": "contract"}
    prepare_result = {"guidance": {"ready": True}, "knowledge": []}
    prepare_key = canonical_digest({"category": "prepare", "identity": prepare_identity})
    entries[prepare_key] = {
        "category": "prepare",
        "fencing_token": 7,
        "identity": prepare_identity,
        "status": "committed",
        "result": prepare_result,
        "result_digest": canonical_digest(prepare_result),
    }

    tool_identity = {
        "step": "initial_retrieval",
        "role": "generator",
        "name": "search_knowledge",
        "call_id": "controller-initial-retrieval",
        "arguments": {"query": "事实", "top_k": 1},
    }
    tool_result = {
        "result": {"items": [], "truncated": False, "next_cursor": None},
        "tool_state": {"registry": {}},
    }
    tool_key = canonical_digest({"category": "tool", "identity": tool_identity})
    entries[tool_key] = {
        "category": "tool",
        "fencing_token": 7,
        "identity": tool_identity,
        "status": "committed",
        "result": tool_result,
        "result_digest": canonical_digest(tool_result),
    }

    for index, result in enumerate(results):
        identity = {
            "role": "generator",
            "context": {"candidate_version": 1, "tool_results": []},
            "turn": index,
        }
        key = canonical_digest({"category": "model", "identity": identity})
        entries[key] = {
            "category": "model",
            "fencing_token": 7,
            "identity": identity,
            "status": "committed",
            "result": deepcopy(result),
            "result_digest": canonical_digest(result),
        }

    return {
        "version": JOURNAL_VERSION,
        "attempts": {"model": len(results), "tool": 1, "prepare": 1},
        "entries": entries,
    }


def _protocol_document(*, model_results: list[dict] | None = None) -> dict:
    return {
        "state": "needs_review",
        "terminal_reason": "invalid_review_or_candidate",
        "candidate_version": 0,
        "execution_journal": _journal(model_results),
    }


def _generator_entries(document: dict) -> list[dict]:
    return [
        entry for entry in document["execution_journal"]["entries"].values()
        if entry.get("category") == "model"
    ]


def test_can_resume_protocol_accepts_only_the_saved_single_tool_envelope():
    assert not can_resume_protocol(_protocol_document())
    assert can_resume_protocol(_protocol_document(), unknown_requests=0)


def test_can_resume_protocol_rejects_digest_mismatch_and_extra_result_fields():
    digest_mismatch = _protocol_document()
    entry = _generator_entries(digest_mismatch)[0]
    entry["result_digest"] = "0" * 64
    assert not can_resume_protocol(digest_mismatch, unknown_requests=0)

    extra_field = _protocol_document()
    entry = _generator_entries(extra_field)[0]
    entry["result"]["extra"] = "不属于ToolCall"
    entry["result_digest"] = canonical_digest(entry["result"])
    assert not can_resume_protocol(extra_field, unknown_requests=0)


def test_can_resume_protocol_rejects_unrelated_review_states_and_existing_candidates():
    arbitrary_review = _protocol_document()
    arbitrary_review["terminal_reason"] = "final_review_rejected"
    assert not can_resume_protocol(arbitrary_review, unknown_requests=0)

    existing_candidate = _protocol_document()
    existing_candidate["candidate_version"] = 1
    existing_candidate["candidate"] = {"version": 1}
    assert not can_resume_protocol(existing_candidate, unknown_requests=0)


def test_can_resume_protocol_requires_exactly_one_generator_model_entry():
    two_generators = _protocol_document(
        model_results=[_model_result("first"), _model_result("second")],
    )
    assert not can_resume_protocol(two_generators, unknown_requests=0)
    assert not can_resume_protocol(_protocol_document(), unknown_requests=1)


def _run_row(store, run_id: str) -> dict:
    with store.connection() as conn:
        return conn.execute(
            "SELECT state,state_version,last_event_seq,fencing_token,lease_owner,"
            "lease_expires_at,document FROM report_runs WHERE run_id=%s",
            (run_id,),
        ).fetchone()


def _service_run(pg_store, *, max_active_runs: int | None = None):
    store, owner, other, session_id = pg_store
    runtime = dependencies(SyntheticRoles())
    if max_active_runs is not None:
        runtime = replace(runtime, max_active_runs=max_active_runs)
    service = ReportRunService(store, runtime)
    run = service.create(owner, CreateRunRequest(
        request_id=uuid4(),
        session_id=session_id,
        accident_data={"事实": "仅用于协议恢复测试"},
        evidence_revision=0,
    ))
    with store.connection() as conn:
        document = deepcopy(conn.execute(
            "SELECT document FROM report_runs WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()["document"])
    document.update({
        "review_status": "failed",
        "terminal_reason": "invalid_review_or_candidate",
        "candidate_version": 0,
        "execution_journal": _journal(),
    })
    document.pop("candidate", None)
    document.pop("candidate_history", None)
    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET state='needs_review',state_version=4,"
            "last_event_seq=8,fencing_token=9,lease_owner=NULL,"
            "lease_expires_at=NULL,document=%s WHERE run_id=%s",
            (Jsonb(document), run["run_id"]),
        )
    return service, store, owner, other, run["run_id"]


def test_pg_protocol_resume_preserves_owner_cas_and_original_journal(pg_store):
    service, store, owner, _, run_id = _service_run(pg_store)
    before = _run_row(store, run_id)
    original_journal = deepcopy(before["document"]["execution_journal"])

    token = service.resume_claim(
        owner, run_id, before["state_version"], retry_unknown_requests=False,
    )

    after = _run_row(store, run_id)
    assert token == before["fencing_token"] + 1
    assert after["state"] == "preparing"
    assert after["state_version"] == before["state_version"] + 1
    assert after["last_event_seq"] == before["last_event_seq"] + 1
    assert after["fencing_token"] == token
    assert after["lease_owner"] is not None
    assert after["document"]["execution_journal"] == original_journal
    assert after["document"]["terminal_reason"] is None

    with store.connection() as conn:
        event = conn.execute(
            "SELECT type,state_version,data FROM report_run_events "
            "WHERE run_id=%s ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    assert event["type"] == "checkpoint"
    assert event["state_version"] == after["state_version"]
    assert event["data"]["from_state"] == "needs_review"
    assert event["data"]["recovery_kind"] == "single_tool_envelope"


def test_pg_protocol_resume_rejects_wrong_owner_and_cas_without_mutation(pg_store):
    _, store, owner, other, run_id = _service_run(pg_store)
    before = _run_row(store, run_id)

    with pytest.raises(HarnessError) as wrong_owner:
        RunRecovery(store).resume_claim(
            other, run_id, before["state_version"], uuid4(),
            retry_unknown_requests=False, validate=lambda _: None,
            minimum_requests=0, minimum_tokens=0,
        )
    assert wrong_owner.value.code == "not_found"
    assert _run_row(store, run_id) == before

    with pytest.raises(HarnessError) as wrong_version:
        RunRecovery(store).resume_claim(
            owner, run_id, before["state_version"] + 1, uuid4(),
            retry_unknown_requests=False, validate=lambda _: None,
            minimum_requests=0, minimum_tokens=0,
        )
    assert wrong_version.value.code == "version_conflict"
    assert _run_row(store, run_id) == before


def test_pg_protocol_resume_honors_active_run_capacity(pg_store):
    service, store, owner, _, run_id = _service_run(pg_store, max_active_runs=1)
    active = service.create(owner, CreateRunRequest(
        request_id=uuid4(),
        session_id=pg_store[3],
        accident_data={"事实": "占用活动运行容量"},
        evidence_revision=0,
    ))
    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET state='preparing',state_version=1,"
            "fencing_token=1,lease_owner=%s,"
            "lease_expires_at=clock_timestamp()+interval '30 seconds' "
            "WHERE run_id=%s",
            (uuid4(), active["run_id"]),
        )
    before = _run_row(store, run_id)

    with pytest.raises(HarnessError) as error:
        service.resume_claim(owner, run_id, before["state_version"], retry_unknown_requests=False)
    assert error.value.code == "server_busy"
    assert _run_row(store, run_id) == before


def test_pg_protocol_resume_cannot_replace_a_new_queued_run_in_the_session(pg_store):
    service, store, owner, _, run_id = _service_run(pg_store)
    service.create(owner, CreateRunRequest(
        request_id=uuid4(), session_id=pg_store[3],
        accident_data={"事实": "较新的排队运行"}, evidence_revision=0,
    ))
    before = _run_row(store, run_id)
    with pytest.raises(HarnessError, match="active_run_conflict"):
        service.resume_claim(owner, run_id, before["state_version"], retry_unknown_requests=False)
    assert _run_row(store, run_id) == before


def test_pg_protocol_authorization_only_reconfirms_identical_bindings(pg_store):
    service, store, owner, _, run_id = _service_run(pg_store)
    before = _run_row(store, run_id)
    proof = {"snapshot_digest": before["document"]["snapshot_digest"], "binding": "合成绑定"}
    document = deepcopy(before["document"])
    document["approval"] = {
        **proof, "policy_digest": document["policy_digest"], "owner_user_id": owner,
        "approved_at": "2026-09-20T00:00:00+00:00",
    }
    with store.connection() as conn:
        conn.execute("UPDATE report_runs SET document=%s WHERE run_id=%s", (Jsonb(document), run_id))
    before = _run_row(store, run_id)
    # 此测试仅覆盖终态重新授权的数据库边界；目录签发本身由既有授权测试覆盖。
    service.dependencies = replace(
        service.dependencies,
        authorization_catalog=SimpleNamespace(validate=lambda _record, _request: deepcopy(proof)),
    )
    result = service.authorize(owner, run_id, SimpleNamespace())
    assert result["can_resume_protocol"] is True
    assert _run_row(store, run_id) == before
    proof["binding"] = "变化后的合成绑定"
    with pytest.raises(HarnessError, match="authorization_stale"):
        service.authorize(owner, run_id, SimpleNamespace())
    assert _run_row(store, run_id) == before


def test_pg_protocol_resume_rejects_unknown_requests_and_retry_opt_in(pg_store):
    _, store, owner, _, run_id = _service_run(pg_store)
    request_id = uuid4()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO report_run_requests(request_id,run_id,attempt_id,fencing_token,"
            "role,endpoint_digest,request_digest,status,reserved_tokens,actual_tokens,result) "
            "VALUES (%s,%s,%s,9,'generator',%s,%s,'completion_unknown',10,NULL,NULL)",
            (request_id, run_id, uuid4(), "e" * 64, "r" * 64),
        )
    before = _run_row(store, run_id)

    with pytest.raises(HarnessError) as unknown:
        RunRecovery(store).resume_claim(
            owner, run_id, before["state_version"], uuid4(),
            retry_unknown_requests=False, validate=lambda _: None,
            minimum_requests=0, minimum_tokens=0,
        )
    assert unknown.value.code == "completion_unknown"
    assert _run_row(store, run_id) == before

    with pytest.raises(HarnessError) as retry:
        RunRecovery(store).resume_claim(
            owner, run_id, before["state_version"], uuid4(),
            retry_unknown_requests=True, validate=lambda _: None,
            minimum_requests=0, minimum_tokens=0,
        )
    assert retry.value.code == "invalid_resume_request"
    assert _run_row(store, run_id) == before
