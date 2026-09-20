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
from app.report_harness.recovery import (
    RunRecovery, can_resume_protocol, can_resume_tool_contract,
)
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


def _tool_contract_candidate() -> dict:
    return {
        "version": 1,
        "report_markdown": "完整候选",
        "claims": [],
        "obligation_resolutions": [],
        "issue_responses": [],
    }


def _tool_contract_journal(
    candidate: dict, *, denied_count: int = 2, generator_history: bool = False,
) -> dict:
    entries = {}

    def committed(category: str, identity: dict, result: dict) -> None:
        key = canonical_digest({"category": category, "identity": identity})
        entries[key] = {
            "category": category,
            "fencing_token": 9,
            "identity": identity,
            "status": "committed",
            "result": deepcopy(result),
            "result_digest": canonical_digest(result),
        }

    prepare_identity = {"snapshot": "snapshot", "contract": "contract"}
    committed("prepare", prepare_identity, {"guidance": {"ready": True}, "knowledge": []})
    committed(
        "tool",
        {
            "step": "initial_retrieval", "role": "generator", "name": "search_knowledge",
            "call_id": "controller-initial-retrieval",
            "arguments": {"query": "事实", "top_k": 1},
        },
        {"result": {"items": [], "truncated": False, "next_cursor": None},
         "tool_state": {"registry": {}}},
    )
    generator_results = (
        [{"claim_id": "arbitrary-claim", "quote": "合成片段", "type": "fact",
          "evidence_refs": [], "knowledge_refs": []},
         {"protocol_error": "invalid_role_response"}, candidate]
        if generator_history else [candidate]
    )
    for turn, result in enumerate(generator_results):
        context = {"candidate_version": 1, "tool_results": []}
        if turn:
            context["protocol_feedback"] = {
                "repairs": [{"response_digest": canonical_digest(prior), "errors": []}
                            for prior in generator_results[:turn]],
            }
        committed(
            "model",
            {"role": "generator", "context": context, "turn": turn},
            result,
        )
    reviewer_context = {
        "snapshot": {"case": "snapshot"},
        "snapshot_digest": "s" * 64,
        "candidate": deepcopy(candidate),
        "candidate_digest": canonical_digest(candidate),
        "unresolved_issues": [],
        "tool_results": [],
    }
    calls = [
        {"call_id": "review-read", "name": "read_evidence", "arguments": {"evidence_ids": []}},
        *[
            {"call_id": f"review-search-{index}", "name": "search_knowledge",
             "arguments": {"query": "合成", "top_k": 5}}
            for index in range(denied_count)
        ],
    ]
    committed(
        "model",
        {"role": "reviewer", "context": reviewer_context},
        {"tool_calls": calls},
    )
    committed(
        "tool",
        {"role": "reviewer", "context_digest": "r" * 64, "call_id": "review-read",
         "name": "read_evidence", "arguments": {"evidence_ids": []}},
        {"result": {"records": []}, "tool_state": {"registry": {}}},
    )
    for index in range(denied_count):
        identity = {
            "role": "reviewer", "context_digest": "r" * 64,
            "call_id": f"review-search-{index}", "name": "search_knowledge",
            "arguments": {"query": "合成", "top_k": 5},
        }
        key = canonical_digest({"category": "tool", "identity": identity})
        entries[key] = {
            "category": "tool", "fencing_token": 9, "identity": identity,
            "status": "denied", "code": "retrieval_policy_exceeded",
        }
    return {
        "version": JOURNAL_VERSION,
        "attempts": {"model": len(generator_results) + 1,
                      "tool": 2 + denied_count, "prepare": 1},
        "entries": entries,
    }


def _tool_contract_document(*, denied_count: int = 2, generator_history: bool = False) -> dict:
    candidate = _tool_contract_candidate()
    digest = canonical_digest(candidate)
    return {
        "state": "failed",
        "terminal_reason": "retrieval_policy_exceeded",
        "review_status": "failed",
        "candidate_version": 1,
        "candidate": deepcopy(candidate),
        "candidate_history": [{"version": 1, "digest": digest, "candidate": deepcopy(candidate)}],
        "execution_journal": _tool_contract_journal(
            candidate, denied_count=denied_count, generator_history=generator_history,
        ),
    }


def _generator_entries(document: dict) -> list[dict]:
    return [
        entry for entry in document["execution_journal"]["entries"].values()
        if entry.get("category") == "model"
    ]


def test_can_resume_protocol_accepts_only_the_saved_single_tool_envelope():
    assert not can_resume_protocol(_protocol_document())
    assert can_resume_protocol(_protocol_document(), unknown_requests=0)


def test_can_resume_protocol_rejects_digest_mismatch_but_allows_known_structure_repairs():
    digest_mismatch = _protocol_document()
    entry = _generator_entries(digest_mismatch)[0]
    entry["result_digest"] = "0" * 64
    assert not can_resume_protocol(digest_mismatch, unknown_requests=0)

    extra_field = _protocol_document()
    entry = _generator_entries(extra_field)[0]
    entry["result"]["extra"] = "不属于ToolCall"
    entry["result_digest"] = canonical_digest(entry["result"])
    assert can_resume_protocol(extra_field, unknown_requests=0)


def test_can_resume_protocol_rejects_unrelated_review_states_and_existing_candidates():
    arbitrary_review = _protocol_document()
    arbitrary_review["terminal_reason"] = "final_review_rejected"
    assert not can_resume_protocol(arbitrary_review, unknown_requests=0)

    existing_candidate = _protocol_document()
    existing_candidate["candidate_version"] = 1
    existing_candidate["candidate"] = {"version": 1}
    assert not can_resume_protocol(existing_candidate, unknown_requests=0)


def test_can_resume_protocol_handles_multiple_known_turns_but_never_a_valid_candidate():
    two_generators = _protocol_document(
        model_results=[_model_result("first"), {"claim_id": "C8", "quote": "合成片段"}],
    )
    assert can_resume_protocol(two_generators, unknown_requests=0)
    complete = _protocol_document(model_results=[
        _model_result("first"), {"version": 1, "report_markdown": "合成完整候选"},
    ])
    assert not can_resume_protocol(complete, unknown_requests=0)
    assert not can_resume_protocol(_protocol_document(), unknown_requests=1)


def test_can_resume_tool_contract_accepts_bound_candidate_and_multiple_denials():
    document = _tool_contract_document(denied_count=2)

    assert can_resume_tool_contract(document, unknown_requests=0)


def test_can_resume_tool_contract_allows_known_generator_structure_repair_history():
    document = _tool_contract_document(generator_history=True)

    assert can_resume_tool_contract(document, unknown_requests=0)


@pytest.mark.parametrize("omitted_field", ["knowledge_refs", "evidence_refs"])
def test_tool_contract_recovery_binds_normalized_candidate_without_rewriting_raw_response(omitted_field):
    candidate = _tool_contract_candidate()
    candidate["claims"] = [{
        "claim_id": "fact-one", "type": "fact", "text_span": {"start": 0, "end": 2},
        "evidence_refs": [], "knowledge_refs": [],
    }]
    digest = canonical_digest(candidate)
    document = _tool_contract_document()
    document.update({
        "candidate": deepcopy(candidate),
        "candidate_history": [{"version": 1, "digest": digest, "candidate": deepcopy(candidate)}],
        "execution_journal": _tool_contract_journal(candidate, generator_history=True),
    })
    entry = next(item for item in document["execution_journal"]["entries"].values()
                 if item.get("result_digest") == digest)
    del entry["result"]["claims"][0][omitted_field]
    entry["result_digest"] = canonical_digest(entry["result"])
    before = deepcopy(document)
    assert entry["result_digest"] != digest
    assert can_resume_tool_contract(document, unknown_requests=0)
    assert document == before


@pytest.mark.parametrize("mutation", ["missing", "unbound", "too_many"])
def test_tool_contract_recovery_requires_saved_candidate_repair_provenance(mutation):
    document = _tool_contract_document(generator_history=True)
    entries = document["execution_journal"]["entries"]
    key, entry = next((key, item) for key, item in entries.items()
                      if item.get("result_digest") == canonical_digest(document["candidate"]))
    context = entry["identity"]["context"]
    if mutation == "missing":
        context.pop("protocol_feedback")
    elif mutation == "unbound":
        context["protocol_feedback"]["repairs"][0]["response_digest"] = "f" * 64
    else:
        context["protocol_feedback"]["repairs"].append({"response_digest": "f" * 64})
    del entries[key]
    entries[canonical_digest({"category": entry["category"], "identity": entry["identity"]})] = entry
    assert not can_resume_tool_contract(document, unknown_requests=0)


@pytest.mark.parametrize("replacement", ["missing_result", "candidate_itself"])
def test_tool_contract_recovery_rejects_an_extra_unbound_repair_digest(replacement):
    document = _tool_contract_document(generator_history=True)
    entries = document["execution_journal"]["entries"]
    marker = {"protocol_error": "invalid_role_response"}
    marker_key = next(key for key, entry in entries.items() if entry.get("result") == marker)
    del entries[marker_key]
    digest = canonical_digest(document["candidate"])
    key, entry = next((key, item) for key, item in entries.items()
                      if item.get("result_digest") == digest)
    repairs = entry["identity"]["context"]["protocol_feedback"]["repairs"]
    repairs[1]["response_digest"] = "f" * 64 if replacement == "missing_result" else digest
    del entries[key]
    entries[canonical_digest({"category": entry["category"], "identity": entry["identity"]})] = entry
    assert not can_resume_tool_contract(document, unknown_requests=0)


@pytest.mark.parametrize("mutation", [
    "candidate", "generator_result", "review_result", "denied_category",
    "denied_code", "unknown",
])
def test_can_resume_tool_contract_rejects_unbound_or_unsafe_history(mutation):
    document = _tool_contract_document()
    if mutation == "candidate":
        document["candidate"]["report_markdown"] = "被篡改候选"
    elif mutation == "generator_result":
        entry = next(
            item for item in document["execution_journal"]["entries"].values()
            if item["category"] == "model" and item["identity"]["role"] == "generator"
        )
        entry["result"]["report_markdown"] = "被篡改生成结果"
        entry["result_digest"] = canonical_digest(entry["result"])
    elif mutation == "review_result":
        entry = next(
            item for item in document["execution_journal"]["entries"].values()
            if item["category"] == "model" and item["identity"]["role"] == "reviewer"
        )
        entry["result"] = {
            "candidate_digest": canonical_digest(document["candidate"]),
            "snapshot_digest": "s" * 64,
            "coverage_checks": [], "issues": [], "completed_checks": [],
        }
        entry["result_digest"] = canonical_digest(entry["result"])
    elif mutation == "denied_category":
        entry = next(
            item for item in document["execution_journal"]["entries"].values()
            if item["status"] == "denied"
        )
        entry["category"] = "model"
        entry["identity"] = {"role": "generator", "context": {"candidate_version": 1}}
    elif mutation == "denied_code":
        entry = next(
            item for item in document["execution_journal"]["entries"].values()
            if item["status"] == "denied"
        )
        entry["code"] = "tool_not_allowed"
    else:
        assert mutation == "unknown"

    unknown_requests = 1 if mutation == "unknown" else 0
    assert not can_resume_tool_contract(document, unknown_requests=unknown_requests)


def test_can_resume_tool_contract_rejects_general_failed_and_missing_denial():
    general_failed = _tool_contract_document()
    general_failed["terminal_reason"] = "execution_error"
    assert not can_resume_tool_contract(general_failed, unknown_requests=0)

    no_denied = _tool_contract_document(denied_count=0)
    assert not can_resume_tool_contract(no_denied, unknown_requests=0)

    extra_history = _tool_contract_document()
    extra_history["candidate_history"].append(deepcopy(extra_history["candidate_history"][0]))
    assert not can_resume_tool_contract(extra_history, unknown_requests=0)


def test_public_view_keeps_can_resume_protocol_compatibility_for_tool_contract():
    document = {
        **_tool_contract_document(),
        "run_id": str(uuid4()), "session_id": "session", "state_version": 54,
        "snapshot_digest": "s" * 64, "last_event_seq": 53,
        "quality_gate": "engineering_only", "formal_export_eligible": False,
        "release_binding_status": "unapproved", "budget": {"unknown_requests": 0},
    }

    result = ReportRunService.public_view(document)

    assert result["can_resume_protocol"] is True


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


def _service_tool_contract_run(pg_store):
    store, owner, other, session_id = pg_store
    service = ReportRunService(store, dependencies(SyntheticRoles()))
    run = service.create(owner, CreateRunRequest(
        request_id=uuid4(),
        session_id=session_id,
        accident_data={"事实": "检索策略恢复测试"},
        evidence_revision=0,
    ))
    with store.connection() as conn:
        document = deepcopy(conn.execute(
            "SELECT document FROM report_runs WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()["document"])
    document.update({
        key: value for key, value in _tool_contract_document().items() if key != "state"
    })
    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET state='failed',state_version=54,"
            "last_event_seq=53,fencing_token=9,lease_owner=NULL,"
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
    assert event["data"]["recovery_kind"] == "pre_candidate_protocol"


def test_pg_tool_contract_resume_preserves_candidate_journal_and_event_kind(pg_store):
    service, store, owner, _, run_id = _service_tool_contract_run(pg_store)
    before = _run_row(store, run_id)
    original_document = deepcopy(before["document"])

    token = service.resume_claim(
        owner, run_id, before["state_version"], retry_unknown_requests=False,
    )

    after = _run_row(store, run_id)
    assert token == before["fencing_token"] + 1
    assert after["state"] == "preparing"
    assert after["state_version"] == before["state_version"] + 1
    assert after["last_event_seq"] == before["last_event_seq"] + 1
    assert after["fencing_token"] == token
    assert after["document"]["candidate"] == original_document["candidate"]
    assert after["document"]["candidate_history"] == original_document["candidate_history"]
    assert after["document"]["execution_journal"] == original_document["execution_journal"]
    assert after["document"]["terminal_reason"] is None
    assert after["document"]["review_status"] == "pending"

    with store.connection() as conn:
        event = conn.execute(
            "SELECT type,state_version,data FROM report_run_events "
            "WHERE run_id=%s ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    assert event["type"] == "checkpoint"
    assert event["state_version"] == after["state_version"]
    assert event["data"]["from_state"] == "failed"
    assert event["data"]["recovery_kind"] == "tool_contract"


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


@pytest.mark.parametrize("factory", [_service_run, _service_tool_contract_run])
def test_pg_protocol_authorization_only_reconfirms_identical_bindings(pg_store, factory):
    service, store, owner, _, run_id = factory(pg_store)
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


def test_http_tool_contract_authorize_then_resume_preserves_the_existing_run(pg_store):
    """执行器为替身，仅验证真实JWT、HTTP与数据库授权恢复边界。"""
    from tests.test_run_recovery import _api_client

    service, store, owner, _, run_id = _service_tool_contract_run(pg_store)
    before = _run_row(store, run_id)
    document = deepcopy(before["document"])
    proof = {
        "snapshot_digest": document["snapshot_digest"],
        "endpoint_profile_digest": document["endpoint_profile_digest"],
        "approved_knowledge_manifest_digest": service.dependencies.knowledge_manifest_digest,
    }
    document["approval"] = {
        **proof, "policy_digest": document["policy_digest"], "owner_user_id": owner,
        "approved_at": "2026-09-20T00:00:00+00:00",
    }
    with store.connection() as conn:
        conn.execute("UPDATE report_runs SET document=%s WHERE run_id=%s", (Jsonb(document), run_id))
    service.dependencies = replace(
        service.dependencies,
        authorization_catalog=SimpleNamespace(validate=lambda _record, _request: deepcopy(proof)),
    )
    claimed = []

    async def record_claim(current_owner, current_run, token):
        claimed.append((current_owner, current_run, token))
        return service.get(current_owner, current_run)

    service.execute_claimed = record_claim
    with _api_client(pg_store, service) as (client, headers):
        prefix = "/api/v1/report-runs/" + run_id
        visible = client.get(prefix, headers=headers)
        assert visible.status_code == 200
        assert visible.json()["can_resume_protocol"] is True
        approved = client.post(prefix + "/authorize", headers=headers, json={**proof, "confirmed": True})
        assert approved.status_code == 200, approved.text
        assert approved.json()["state_version"] == 54
        resumed = client.post(prefix + "/resume/stream", headers=headers, json={
            "expected_version": approved.json()["state_version"], "retry_unknown_requests": False,
        })
        assert resumed.status_code == 200, resumed.text
        assert '"recovery_kind": "tool_contract"' in resumed.text
    assert len(claimed) == 1 and claimed[0][:2] == (owner, run_id)
    after = _run_row(store, run_id)
    assert after["document"]["candidate"] == document["candidate"]
    assert after["document"]["execution_journal"] == document["execution_journal"]


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
