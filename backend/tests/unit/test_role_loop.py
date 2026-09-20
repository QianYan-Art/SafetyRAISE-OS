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
