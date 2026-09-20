import asyncio
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from app.report_harness.business_workflow import BusinessWorkflow
from app.report_harness.contracts import canonical_digest
from app.report_harness.controlled_tools import ControlledTools
from app.report_harness.errors import HarnessError
from app.report_harness.evidence import freeze_snapshot
from app.report_harness.role_loop import RoleLoop
from app.report_harness.journal import ExecutionJournal
from app.schemas.report_run import CreateRunRequest
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import MemoryStore, SyntheticRoles, dependencies


def workflow(**changes):
    return BusinessWorkflow("合成专家模板", "合成报告模板", **changes)


def source(manifest):
    text = "合成晴天驾驶规则"
    return {"id": "rule-1", "document_id": "rule", "version": "v1",
            "text": text, "digest": canonical_digest(text), "manifest_digest": manifest}


def tools_with_policy(policy):
    snapshot = freeze_snapshot({"天气": "合成晴天"}, [], 0, "manifest")
    return ControlledTools(snapshot, [source("manifest")], retrieval_policy=policy)


def test_business_controller_runs_initial_search_before_generation():
    class Roles(SyntheticRoles):
        async def generate(self, context):
            assert context["initial_knowledge_snippets"][0]["id"] == "rule-1"
            assert "合成工程样本" in context["prepared"]["guidance"]["note"]
            return await super().generate(context)

    roles = Roles()
    base = dependencies(roles)
    deps = replace(base, business_workflow=workflow(),
                   knowledge_chunks=(source(base.knowledge_manifest_digest),))
    store = MemoryStore()
    service = ReportRunService(store, deps)
    run = service.create("owner", CreateRunRequest(
        request_id=uuid4(), session_id="synthetic", accident_data={"天气": "合成晴天"},
        evidence_revision=0,
    ))
    result = asyncio.run(service.execute("owner", run["run_id"], 0))
    assert result["state"] == "published"
    record = store.get("owner", run["run_id"])
    assert record["business_prompts"] == workflow().prompts()
    assert record["initial_knowledge_snippets"][0]["id"] == "rule-1"
    assert "initial_knowledge_snippets" not in roles.review_context
    assert "prepared" not in roles.review_context
    assert roles.calls == ["prepare", "generate", "review"]


def test_business_template_change_invalidates_queued_run():
    roles = SyntheticRoles()
    store = MemoryStore()
    deps = replace(dependencies(roles), business_workflow=workflow())
    service = ReportRunService(store, deps)
    run = service.create("owner", CreateRunRequest(
        request_id=uuid4(), session_id="synthetic", accident_data={"天气": "晴天"},
        evidence_revision=0,
    ))
    changed = ReportRunService(store, replace(
        deps, business_workflow=replace(workflow(), expert_prompt="另一模板"),
    ))
    with pytest.raises(HarnessError, match="authorization_stale"):
        asyncio.run(changed.execute("owner", run["run_id"], 0))
    assert roles.calls == []


def test_retrieval_limits_survive_checkpoint_and_access_reset():
    policy = workflow().retrieval_policy()
    tools = tools_with_policy(policy)
    initial = tools.initial_retrieval("合成晴天", 3)
    tools.reset_access("generator")
    tools.include_knowledge("generator", initial["items"])
    assert tools.accessed_knowledge("generator") == {"rule-1"}
    assert tools.accessed_knowledge("reviewer") == set()
    query = {"query": "合成晴天", "top_k": 3}
    tools.execute("generator", "search_knowledge", query)
    restored = tools_with_policy(policy)
    restored.restore_checkpoint_state(tools.checkpoint_state())
    restored.execute("generator", "search_knowledge", query)
    with pytest.raises(HarnessError, match="retrieval_request_budget_exhausted"):
        restored.execute("generator", "search_knowledge", query)
    restored.execute("reviewer", "search_knowledge", query)


def test_retrieval_constraints_follow_policy_and_current_state():
    tools = tools_with_policy(workflow().retrieval_policy())
    assert tools.retrieval_constraints("generator") == {
        "additional_top_k": 3,
        "max_query_chars": 120,
        "remaining_rounds": 3,
        "remaining_snippets": 9,
    }
    assert tools.retrieval_constraints("reviewer") == {
        "additional_top_k": 3,
        "max_query_chars": 120,
        "remaining_rounds": 2,
        "remaining_snippets": 9,
    }

    tools.initial_retrieval("合成晴天", 3)
    assert tools.retrieval_constraints("generator") == {
        "additional_top_k": 3,
        "max_query_chars": 120,
        "remaining_rounds": 2,
        "remaining_snippets": 8,
    }


def test_retrieval_constraints_do_not_widen_or_exist_without_policy():
    limited = tools_with_policy(workflow(
        initial_top_k=1, additional_top_k=10, max_total_snippets=1,
        max_query_chars=1000,
    ).retrieval_policy())
    limited.initial_retrieval("合成晴天", 1)
    assert limited.retrieval_constraints("generator") == {
        "additional_top_k": 10,
        "max_query_chars": 1000,
        "remaining_rounds": 2,
        "remaining_snippets": 0,
    }

    snapshot = freeze_snapshot({"天气": "合成晴天"}, [], 0, "manifest")
    without_policy = ControlledTools(snapshot, [source("manifest")])
    assert without_policy.retrieval_constraints("generator") is None


def test_retrieval_policy_error_details_are_safe_and_search_is_not_called():
    calls = []

    def search(query, top_k):
        calls.append((query, top_k))
        return [source("manifest")]

    policy = workflow().retrieval_policy()
    snapshot = freeze_snapshot({"天气": "合成晴天"}, [], 0, "manifest")
    tools = ControlledTools(
        snapshot, [source("manifest")], search=search, retrieval_policy=policy,
    )
    tools.initial_retrieval("合成晴天", 3)
    with pytest.raises(HarnessError) as captured:
        tools.execute("generator", "search_knowledge", {"query": "合成", "top_k": 5})

    assert captured.value.code == "retrieval_policy_exceeded"
    assert captured.value.details == {
        "role": "generator",
        "additional_top_k": 3,
        "max_query_chars": 120,
        "remaining_rounds": 2,
        "remaining_snippets": 8,
    }
    assert calls == [("合成晴天", 3)]
def test_initial_query_uses_original_accident_field_priority():
    assert workflow().initial_query({"事故经过": "  左转  碰撞 ", "天气": "晴天"}) == "左转 碰撞"
    with pytest.raises(ValueError):
        workflow().initial_query({"空": ""})


@pytest.mark.parametrize("arguments", [
    {"query": "x" * 121, "top_k": 3},
    {"query": "合成", "top_k": 4},
])
def test_additional_retrieval_enforces_original_query_and_top_k(arguments):
    tools = tools_with_policy(workflow().retrieval_policy())
    tools.initial_retrieval("合成", 3)
    with pytest.raises(HarnessError, match="retrieval_policy_exceeded"):
        tools.execute("generator", "search_knowledge", arguments)


def test_inline_knowledge_cannot_be_forged():
    tools = tools_with_policy(workflow().retrieval_policy())
    forged = {**source("manifest"), "text": "伪造原文"}
    with pytest.raises(HarnessError, match="checkpoint_knowledge_mismatch"):
        tools.include_knowledge("generator", [forged])


def test_initial_search_journal_replays_without_searching_again():
    class Journal:
        saved = None

        async def invoke(self, kind, request, operation, limit):
            assert kind == "tool" and limit == 24
            assert request["step"] == "initial_retrieval"
            if self.saved is None:
                self.saved = await operation()
            return deepcopy(self.saved)

        def attempts(self, category):
            assert category == "tool"
            return 1

    journal = Journal()
    first = tools_with_policy(workflow().retrieval_policy())
    loop = RoleLoop(first, lambda *args: None, before_call=lambda: None, journal=journal)
    result = asyncio.run(loop.initial_retrieval("合成", 3))
    resumed = tools_with_policy(workflow().retrieval_policy())
    resumed.initial_retrieval = lambda *args: pytest.fail("不应重复首检")
    loop = RoleLoop(resumed, lambda *args: None, before_call=lambda: None, journal=journal)
    assert asyncio.run(loop.initial_retrieval("合成", 3)) == result
    assert resumed.accessed_knowledge("generator") == {"rule-1"}


def test_shared_knowledge_does_not_copy_corpus_into_each_run():
    roles = SyntheticRoles()
    base = dependencies(roles)
    deps = replace(base, business_workflow=workflow(), external_knowledge_source=True,
                   knowledge_chunks=(source(base.knowledge_manifest_digest),))
    store = MemoryStore()
    service = ReportRunService(store, deps)
    run = service.create("owner", CreateRunRequest(
        request_id=uuid4(), session_id="synthetic", accident_data={"天气": "合成晴天"},
        evidence_revision=0,
    ))
    before = store.get("owner", run["run_id"])
    assert before["knowledge_source"] == []
    assert before["knowledge_source_digest"] == canonical_digest(deps.knowledge_chunks)
    assert asyncio.run(service.execute("owner", run["run_id"], 0))["state"] == "published"
    assert store.get("owner", run["run_id"])["knowledge_registry"] == list(deps.knowledge_chunks)


def test_search_unknown_usage_error_keeps_its_original_control_code():
    def search(*args):
        raise HarnessError("usage_unknown")

    snapshot = freeze_snapshot({"天气": "合成晴天"}, [], 0, "manifest")
    tools = ControlledTools(snapshot, [source("manifest")], search=search)
    with pytest.raises(HarnessError, match="usage_unknown"):
        tools.execute("generator", "search_knowledge", {"query": "合成", "top_k": 1})


def test_initial_search_uses_real_journal_contract_and_replays_full_step():
    store = MemoryStore()
    service = ReportRunService(store, dependencies(SyntheticRoles()))
    run = service.create("owner", CreateRunRequest(
        request_id=uuid4(), session_id="synthetic", accident_data={"天气": "合成晴天"},
        evidence_revision=0,
    ))
    run_id = run["run_id"]
    token = service.claim("owner", run_id, 0)
    journal = ExecutionJournal(store, "owner", run_id, token)
    tools = tools_with_policy(workflow().retrieval_policy())
    loop = RoleLoop(tools, lambda *args: None, before_call=lambda: None, journal=journal)
    first = asyncio.run(loop.initial_retrieval("合成晴天", 3))
    record = store.get("owner", run_id)
    assert record["execution_journal"]["attempts"]["tool"] == 1
    assert record["tool_history"][-1]["name"] == "search_knowledge"
    assert record["tool_history"][-1]["status"] == "completed"
    resumed = tools_with_policy(workflow().retrieval_policy())
    resumed.initial_retrieval = lambda *args: pytest.fail("完整首检不能重复执行")
    replay = RoleLoop(
        resumed, lambda *args: None, before_call=lambda: None,
        journal=ExecutionJournal(store, "owner", run_id, token),
    )
    assert asyncio.run(replay.initial_retrieval("合成晴天", 3)) == first
    assert replay.tool_calls == 1
