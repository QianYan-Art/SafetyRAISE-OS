import asyncio

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.role_loop import RoleLoop, normalize_tool_response, role_context


class LocalTools:
    def execute(self, role, name, arguments):
        if name != "read_evidence":
            raise HarnessError("tool_not_allowed")
        return {"records": [{"text": "合成原文", "id": arguments["evidence_ids"][0]}]}


def test_tool_result_is_checkpointed_then_returned_to_role():
    checkpoints = []
    requests = []

    async def role(context):
        requests.append(context)
        if not context["tool_results"]:
            return {"tool_calls": [{
                "call_id": "1", "name": "read_evidence", "arguments": {"evidence_ids": ["e"]},
            }]}
        return {"candidate": "合成"}

    loop = RoleLoop(LocalTools(), lambda public, private: checkpoints.append((public, private)),
                    before_call=lambda: None)
    result = asyncio.run(loop.run("generator", role, {"snapshot": "不变"}))
    assert result == {"candidate": "合成"}
    assert len(requests) == 2
    assert requests[1]["tool_results"][0]["result"]["records"][0]["text"] == "合成原文"
    assert "合成原文" not in str(checkpoints[0][0])
    assert checkpoints[0][0]["status"] == "intent"
    assert checkpoints[1][1]["result"]["records"][0]["text"] == "合成原文"


def test_unknown_capability_is_denied_and_audited():
    checkpoints = []

    async def role(context):
        return {"tool_calls": [{"call_id": "私有调用标识", "name": "私有正文",
                               "arguments": {"command": "bad"}}]}

    loop = RoleLoop(LocalTools(), lambda public, private: checkpoints.append(public),
                    before_call=lambda: None)
    with pytest.raises(HarnessError, match="tool_not_allowed"):
        asyncio.run(loop.run("generator", role, {}))
    assert checkpoints[-1]["status"] == "denied"
    assert "私有" not in str(checkpoints)


def test_tool_loop_is_bounded_even_when_model_never_finishes():
    async def role(context):
        return {"tool_calls": [{
            "call_id": "1", "name": "read_evidence", "arguments": {"evidence_ids": ["e"]},
        }]}

    loop = RoleLoop(LocalTools(), lambda public, private: None,
                    before_call=lambda: None, max_tool_calls=2)
    with pytest.raises(HarnessError, match="tool_budget_exhausted"):
        asyncio.run(loop.run("generator", role, {}))
    assert loop.tool_calls == 2


def test_large_snapshot_only_advertises_directory_without_claiming_read_access():
    snapshot = {
        "accident_data": {"fact": "x" * 100},
        "supplemental_records": [{"evidence_id": "id", "text": "y" * 100, "source_label": "合成"}],
        "fact_obligations": [{"source_refs": ["accident:/fact", "evidence:id"]}],
    }
    context, visible = role_context(snapshot, inline_limit=10)
    assert not visible
    assert context["content_mode"] == "catalogue"
    assert "text" not in context["supplemental_records"][0]
    assert snapshot["supplemental_records"][0]["text"] == "y" * 100


@pytest.mark.parametrize("envelope", [False, True])
def test_single_tool_object_uses_the_same_bounded_dispatch(envelope):
    checkpoints = []
    requests = []

    async def role(context):
        requests.append(context)
        if not context["tool_results"]:
            call = {"call_id": "single", "name": "read_evidence",
                    "arguments": {"evidence_ids": ["e"]}}
            return {"tool_calls": [call]} if envelope else call
        return {"candidate": "合成"}

    loop = RoleLoop(LocalTools(), lambda public, private: checkpoints.append(public),
                    before_call=lambda: None)
    assert asyncio.run(loop.run("generator", role, {})) == {"candidate": "合成"}
    assert loop.model_turns == 2
    assert loop.tool_calls == 1
    assert requests[1]["tool_results"][0]["call_id"] == "single"
    assert [item["status"] for item in checkpoints] == ["intent", "completed"]


def test_single_tool_object_cannot_bypass_capability_policy():
    async def role(context):
        return {"call_id": "single", "name": "shell", "arguments": {"command": "bad"}}

    checkpoints = []
    loop = RoleLoop(LocalTools(), lambda public, private: checkpoints.append(public),
                    before_call=lambda: None)
    with pytest.raises(HarnessError, match="tool_not_allowed"):
        asyncio.run(loop.run("generator", role, {}))
    assert checkpoints[-1]["status"] == "denied"


@pytest.mark.parametrize("response", [
    {"call_id": "one", "name": "read_evidence"},
    {"call_id": "one", "name": "read_evidence", "arguments": {}, "report_markdown": "混合"},
    {"version": 1, "report_markdown": "报告"},
])
def test_normalization_does_not_guess_or_discard_extra_fields(response):
    assert normalize_tool_response(response) is response


@pytest.mark.parametrize("field,value", [("call_id", ""), ("name", ""), ("arguments", [])])
def test_single_tool_object_requires_valid_typed_fields(field, value):
    from pydantic import ValidationError

    response = {"call_id": "one", "name": "read_evidence", "arguments": {}}
    response[field] = value
    with pytest.raises(ValidationError):
        normalize_tool_response(response)


def test_saved_single_tool_response_is_replayed_without_a_new_model_request():
    from copy import deepcopy

    class CheckpointTools(LocalTools):
        def checkpoint_state(self):
            return {"synthetic": True}

        def restore_checkpoint_state(self, state):
            assert state == {"synthetic": True}

    class SavedJournal:
        def __init__(self):
            self.counts = {"model": 1, "tool": 0}

        def attempts(self, category):
            return self.counts[category]

        async def invoke(self, category, identity, operation, **kwargs):
            if category == "model" and not identity["context"]["tool_results"]:
                return deepcopy({"call_id": "saved", "name": "read_evidence",
                                 "arguments": {"evidence_ids": ["e"]}})
            self.counts[category] += 1
            return await operation()

    requests = []

    async def role(context):
        requests.append(context)
        assert context["tool_results"][0]["call_id"] == "saved"
        return {"candidate": "合成"}

    loop = RoleLoop(CheckpointTools(), lambda public, private: None,
                    before_call=lambda: None, journal=SavedJournal())
    assert asyncio.run(loop.run("generator", role, {})) == {"candidate": "合成"}
    assert len(requests) == 1
    assert loop.tool_calls == 1


def _candidate_context():
    from app.report_harness.contracts import CandidateReport

    return {"response_schema": CandidateReport.model_json_schema(), "candidate_version": 1}


def test_fragmented_candidate_gets_bounded_structure_feedback_not_silent_defaults():
    requests = []

    async def role(context):
        requests.append(context)
        if "protocol_feedback" not in context:
            return {"claim_id": "C8", "quote": "不要把此片段当完整报告或回填默认事实"}
        assert "不要把此片段" not in str(context["protocol_feedback"])
        return {"version": 1, "report_markdown": "合成完整正文"}

    loop = RoleLoop(LocalTools(), lambda *_: None, before_call=lambda: None)
    assert asyncio.run(loop.run("generator", role, _candidate_context()))["version"] == 1
    assert len(requests) == 2 and loop.model_turns == 2 and loop.tool_calls == 0
    errors = requests[1]["protocol_feedback"]["repairs"][0]["errors"]
    assert any(item["path"] == ["report_markdown"] for item in errors)


def test_known_decode_failure_is_repairable_but_unknown_completion_is_not():
    calls = []

    async def role(context):
        calls.append(context)
        if len(calls) == 1:
            raise HarnessError("invalid_role_response")
        return {"version": 1, "report_markdown": "合成正文"}

    loop = RoleLoop(LocalTools(), lambda *_: None, before_call=lambda: None)
    asyncio.run(loop.run("generator", role, _candidate_context()))
    assert len(calls) == 2

    async def unknown(context):
        calls.append(context)
        raise HarnessError("completion_unknown")

    with pytest.raises(HarnessError, match="completion_unknown"):
        asyncio.run(loop.run("generator", unknown, _candidate_context()))
    assert len(calls) == 3


@pytest.mark.parametrize("role_name", ["generator", "reviewer"])
def test_structure_repair_stops_after_two_additional_model_turns(role_name):
    calls = []

    async def invalid(context):
        calls.append(context)
        return {"fragment": "合成"}

    loop = RoleLoop(LocalTools(), lambda *_: None, before_call=lambda: None)
    with pytest.raises(HarnessError, match="invalid_role_response"):
        asyncio.run(loop.run(role_name, invalid, _candidate_context()))
    assert len(calls) == 3 and loop.tool_calls == 0


def test_structure_repair_cannot_expand_the_shared_model_turn_budget():
    async def invalid(_context):
        return {"fragment": "合成"}

    loop = RoleLoop(LocalTools(), lambda *_: None, before_call=lambda: None, max_model_turns=2)
    with pytest.raises(HarnessError, match="model_turn_budget_exhausted"):
        asyncio.run(loop.run("generator", invalid, _candidate_context()))
    assert loop.model_turns == 2


def test_structure_repair_feedback_is_stable_after_jsonb_key_reordering():
    import json
    from copy import deepcopy
    from app.report_harness.contracts import canonical_digest

    class CachedJournal:
        def __init__(self):
            self.saved = {}

        def attempts(self, category):
            assert category == "model"
            return len(self.saved)

        async def invoke(self, category, identity, operation, **_kwargs):
            assert category == "model"
            key = canonical_digest(identity)
            if key in self.saved:
                return json.loads(json.dumps(self.saved[key], sort_keys=True))
            response = await operation()
            self.saved[key] = deepcopy(response)
            return response

    requests = []

    async def role(context):
        requests.append(context)
        if "protocol_feedback" not in context:
            return {"z_extra": "合成", "a_extra": "合成"}
        return {"version": 1, "report_markdown": "合成正文"}

    journal = CachedJournal()
    for _ in range(2):
        loop = RoleLoop(LocalTools(), lambda *_: None, before_call=lambda: None, journal=journal)
        assert asyncio.run(loop.run("generator", role, _candidate_context()))["version"] == 1
    assert len(requests) == 2
    assert len(journal.saved) == 2


class PolicyTools(LocalTools):
    def retrieval_constraints(self, role):
        assert role in {"generator", "reviewer"}
        return {"additional_top_k": 3, "max_query_chars": 120,
                "remaining_rounds": 2, "remaining_snippets": 6}

    def execute(self, role, name, arguments):
        if name == "search_knowledge":
            if arguments["top_k"] > 3:
                raise HarnessError("retrieval_policy_exceeded")
            return {"items": []}
        return super().execute(role, name, arguments)


def test_role_sees_actual_search_limits_and_receives_audited_denial_feedback():
    calls, events = [], []

    async def reviewer(context):
        calls.append(context)
        search = next(item for item in context["tools"] if item["name"] == "search_knowledge")
        assert search["parameters"]["properties"]["top_k"]["maximum"] == 3
        assert search["parameters"]["properties"]["query"]["maxLength"] == 120
        if len(calls) == 1:
            return {"tool_calls": [{"call_id": "too-many", "name": "search_knowledge",
                                   "arguments": {"query": "私有查询不回显", "top_k": 5}}]}
        feedback = context["tool_results"][0]["result"]
        assert feedback["error"]["constraints"]["additional_top_k"] == 3
        assert "私有查询" not in str(feedback)
        return {"done": True}

    loop = RoleLoop(PolicyTools(), lambda public, _: events.append(public), before_call=lambda: None)
    assert asyncio.run(loop.run("reviewer", reviewer, {})) == {"done": True}
    assert [item["status"] for item in events] == ["intent", "denied"]
    assert loop.model_turns == 2 and loop.tool_calls == 1


def test_policy_denial_allows_only_two_corrective_model_rounds():
    calls = []

    async def reviewer(context):
        calls.append(context)
        return {"tool_calls": [{"call_id": str(len(calls)), "name": "search_knowledge",
                               "arguments": {"query": "合成", "top_k": 5}}]}

    loop = RoleLoop(PolicyTools(), lambda *_: None, before_call=lambda: None)
    with pytest.raises(HarnessError, match="invalid_role_response"):
        asyncio.run(loop.run("reviewer", reviewer, {}))
    assert len(calls) == 3 and loop.tool_calls == 3


@pytest.mark.parametrize("code", [
    "completion_unknown", "retrieval_request_budget_exhausted", "authorization_stale",
    "resource_pressure", "tool_not_allowed",
])
def test_tool_feedback_never_retries_unknown_budget_access_or_resource_errors(code):
    class DeniedTools(PolicyTools):
        def execute(self, *_):
            raise HarnessError(code)

    calls = []

    async def reviewer(context):
        calls.append(context)
        return {"tool_calls": [{"call_id": "one", "name": "search_knowledge",
                               "arguments": {"query": "合成", "top_k": 1}}]}

    loop = RoleLoop(DeniedTools(), lambda *_: None, before_call=lambda: None)
    with pytest.raises(HarnessError, match=code):
        asyncio.run(loop.run("reviewer", reviewer, {}))
    assert len(calls) == 1


def test_no_search_is_advertised_when_actual_role_budget_is_empty():
    from app.report_harness.role_loop import tool_schemas

    for field in ("remaining_rounds", "remaining_snippets"):
        policy = PolicyTools().retrieval_constraints("reviewer")
        policy[field] = 0
        assert "search_knowledge" not in {item["name"] for item in tool_schemas(policy)}


def test_rejected_final_response_gets_itemized_feedback_and_may_read_before_resubmitting():
    from app.report_harness.role_loop import ResponseRejected

    requests, read = [], set()

    class ReadingTools(LocalTools):
        def execute(self, role, name, arguments):
            read.update(arguments["evidence_ids"])
            return super().execute(role, name, arguments)

    async def role(context):
        requests.append(context)
        if "protocol_feedback" in context and not context["tool_results"]:
            return {"tool_calls": [{"call_id": "read-1", "name": "read_evidence",
                                    "arguments": {"evidence_ids": ["accident:/天气"]}}]}
        return {"version": 1, "report_markdown": "合成正文"}

    def accept(_candidate):
        if "accident:/天气" not in read:
            raise ResponseRejected("source_not_read", [
                (("claims", 0, "evidence_refs"), "accident:/天气：本轮尚未读取该事实原文"),
            ])

    loop = RoleLoop(ReadingTools(), lambda *_: None, before_call=lambda: None)
    assert asyncio.run(loop.run("generator", role, _candidate_context(), accept=accept))["version"] == 1
    assert len(requests) == 3 and loop.tool_calls == 1
    assert requests[1]["protocol_feedback"]["repairs"][0]["errors"] == [{
        "type": "source_not_read", "path": ["claims", 0, "evidence_refs"],
        "message": "accident:/天气：本轮尚未读取该事实原文",
    }]
    assert requests[2]["protocol_feedback"] == requests[1]["protocol_feedback"]


def test_rejection_that_is_never_fixed_stops_after_two_repairs():
    from app.report_harness.role_loop import ResponseRejected

    calls = []

    async def role(context):
        calls.append(context)
        return {"version": 1, "report_markdown": "合成正文"}

    def accept(_candidate):
        raise ResponseRejected("source_not_read", [((), "synthetic-rule：本轮尚未读取")])

    loop = RoleLoop(LocalTools(), lambda *_: None, before_call=lambda: None)
    with pytest.raises(HarnessError, match="invalid_role_response") as raised:
        asyncio.run(loop.run("generator", role, _candidate_context(), accept=accept))
    assert len(calls) == 3
    assert isinstance(raised.value.__cause__, ResponseRejected)
