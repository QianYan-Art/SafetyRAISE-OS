import asyncio
from uuid import uuid4

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.journal import ExecutionJournal
from app.report_harness.money_guard import MoneyGuardNotSent
from app.report_harness.role_loop import RoleLoop
from app.report_harness.review_ledger import IssueLedger
from app.report_harness.recovery import RunRecovery
from app.report_harness.store import RunStore
from app.schemas.report_run import BudgetPolicy
from tests.test_request_ledger import create_run
from tests.unit.test_controlled_tools import make_tools
from tests.unit.test_review_ledger import issue, review


def test_reconstructed_journal_reuses_model_and_tool_results_without_invocation(pg_store):
    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    tools, _ = make_tools()
    invoked = []

    async def model(context):
        invoked.append(context)
        if not context["tool_results"]:
            return {"tool_calls": [{
                "call_id": "synthetic-search", "name": "search_knowledge",
                "arguments": {"query": "规则", "top_k": 1},
            }]}
        return {"done": True}

    def make_loop(current_store, current_tools):
        return RoleLoop(
            current_tools, lambda *_: pytest.fail("日志路径不应再用非原子旧检查点"),
            before_call=lambda: current_store.assert_active(owner, run_id, token),
            max_model_turns=2, max_tool_calls=1,
            journal=ExecutionJournal(current_store, owner, run_id, token),
        )

    loop = make_loop(store, tools)
    assert asyncio.run(loop.run("generator", model, {"synthetic": "固定上下文"})) == {"done": True}
    assert len(invoked) == 2
    assert tools.accessed_knowledge("generator")
    before = store.events(owner, run_id)
    rebuilt_store = RunStore(store.connection)
    rebuilt_tools, _ = make_tools()

    async def forbidden_model(_):
        pytest.fail("完整持久模型结果不应重新调用")

    def forbidden_tool(*_):
        pytest.fail("完整持久工具结果不应重新执行")

    rebuilt_tools.execute = forbidden_tool
    replay = make_loop(rebuilt_store, rebuilt_tools)
    assert asyncio.run(replay.run(
        "generator", forbidden_model, {"synthetic": "固定上下文"},
    )) == {"done": True}
    assert rebuilt_tools.accessed_knowledge("generator") == tools.accessed_knowledge("generator")
    assert rebuilt_tools.accessed_knowledge("reviewer") == set()
    assert store.events(owner, run_id) == before
    assert replay.journal.attempts("model") == 2
    assert replay.journal.attempts("tool") == 1


def test_checkpoint_state_is_bound_to_frozen_snapshot():
    original, _ = make_tools()
    different, _ = make_tools(accident={"事实": "不同输入"})
    with pytest.raises(HarnessError, match="checkpoint_snapshot_mismatch"):
        different.restore_checkpoint_state(original.checkpoint_state())


def test_issue_ids_are_stable_for_replay_and_scoped_to_run():
    run_id = str(uuid4())
    source = review(issue())
    first = IssueLedger(namespace=run_id).apply(source)
    replay = IssueLedger(namespace=run_id).apply(source)
    other = IssueLedger(namespace=str(uuid4())).apply(source)
    assert first.issues[0].issue_id == replay.issues[0].issue_id
    assert first.issues[0].issue_id != other.issues[0].issue_id


def test_incomplete_step_retry_preserves_attempt_budget(pg_store):
    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    journal = ExecutionJournal(store, owner, run_id, token)

    async def interrupted():
        raise RuntimeError("合成步骤未提交")

    with pytest.raises(RuntimeError):
        asyncio.run(journal.invoke("model", {"step": "fixed"}, interrupted, limit=1))
    rebuilt = ExecutionJournal(store, owner, run_id, token)
    with pytest.raises(HarnessError, match="completion_unknown"):
        asyncio.run(rebuilt.invoke("model", {"step": "fixed"}, interrupted, limit=2))
    suspended = store.transition(owner, run_id, token, "suspended", {})
    new_token = RunRecovery(store).resume_claim(
        owner, run_id, suspended["state_version"], uuid4(),
        retry_unknown_requests=False, validate=lambda _: None,
        minimum_requests=0, minimum_tokens=0,
    )
    rebuilt = ExecutionJournal(store, owner, run_id, new_token)
    with pytest.raises(HarnessError, match="model_turn_budget_exhausted"):
        asyncio.run(rebuilt.invoke("model", {"step": "fixed"}, interrupted, limit=1))


def test_model_context_reuses_only_tool_description_changes(pg_store):
    from copy import deepcopy

    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    journal = ExecutionJournal(store, owner, run_id, token)
    original = {"snapshot": {"fixed": True}, "tools": [{"maximum": 10}], "tool_results": []}

    async def model():
        return {"done": True}

    asyncio.run(journal.invoke("model", {"role": "generator", "context": original}, model))
    rebuilt = ExecutionJournal(store, owner, run_id, token)
    updated = {**deepcopy(original), "tools": [{"maximum": 3}]}
    assert rebuilt.model_context("generator", updated) == original
    assert rebuilt.model_context("reviewer", updated) == updated
    different = {**updated, "snapshot": {"fixed": False}}
    assert rebuilt.model_context("generator", different) == different
    assert updated["tools"] == [{"maximum": 3}]

    asyncio.run(journal.invoke("model", {"role": "generator", "context": updated}, model))
    ambiguous = ExecutionJournal(store, owner, run_id, token)
    assert ambiguous.model_context("generator", updated) == updated
    with pytest.raises(HarnessError, match="checkpoint_context_ambiguous"):
        ambiguous.model_context("generator", {**updated, "tools": [{"maximum": 2}]})


def test_saved_pre_search_denial_is_not_reexecuted_or_rewritten(pg_store):
    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    journal = ExecutionJournal(store, owner, run_id, token)
    identity = {"role": "reviewer", "context_digest": "a" * 64, "call_id": "denied",
                "name": "search_knowledge", "arguments": {"query": "合成", "top_k": 5}}
    calls = []

    async def denied():
        calls.append(True)
        raise HarnessError("retrieval_policy_exceeded")

    with pytest.raises(HarnessError, match="retrieval_policy_exceeded"):
        asyncio.run(journal.invoke("tool", identity, denied, limit=1))
    before = store.events(owner, run_id)
    rebuilt = ExecutionJournal(store, owner, run_id, token)
    with pytest.raises(HarnessError, match="retrieval_policy_exceeded"):
        asyncio.run(rebuilt.invoke("tool", identity, denied, limit=1))
    assert calls == [True]
    assert rebuilt.attempts("tool") == 1
    assert store.events(owner, run_id) == before
    assert list(rebuilt._journal["entries"].values())[0]["status"] == "denied"


def test_model_money_guard_denial_does_not_leave_unknown_intent(pg_store):
    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    journal = ExecutionJournal(store, owner, run_id, token)
    identity = {"role": "expert", "context_digest": "a" * 64}

    async def denied():
        raise MoneyGuardNotSent("unknown_cost_ack_required")

    with pytest.raises(MoneyGuardNotSent):
        asyncio.run(journal.invoke("model", identity, denied, limit=2))
    rebuilt = ExecutionJournal(store, owner, run_id, token)
    assert list(rebuilt._journal["entries"].values())[0]["status"] == "denied"
    with pytest.raises(MoneyGuardNotSent):
        asyncio.run(rebuilt.invoke("model", identity, denied, limit=2))


def test_legacy_tool_contract_recovery_reuses_models_and_denials_across_restart(pg_store):
    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    model_calls, tool_calls = [], []

    class ContractTools:
        def __init__(self, upgraded):
            self.upgraded = upgraded

        def retrieval_constraints(self, role):
            if not self.upgraded:
                return None
            return {"additional_top_k": 3, "max_query_chars": 120,
                    "remaining_rounds": 2, "remaining_snippets": 6}

        def execute(self, *_):
            tool_calls.append(True)
            raise HarnessError("retrieval_policy_exceeded")

    async def model(context):
        model_calls.append(context)
        if not context["tool_results"]:
            return {"tool_calls": [{"call_id": "old-five", "name": "search_knowledge",
                                   "arguments": {"query": "合成", "top_k": 5}}]}
        search = next(item for item in context["tools"] if item["name"] == "search_knowledge")
        assert search["parameters"]["properties"]["top_k"]["maximum"] == 3
        assert context["tool_results"][0]["result"]["error"]["code"] == "retrieval_policy_exceeded"
        return {"done": True}

    def loop(upgraded):
        return RoleLoop(
            ContractTools(upgraded), lambda *_: pytest.fail("使用持久日志"),
            before_call=lambda: store.assert_active(owner, run_id, token),
            journal=ExecutionJournal(store, owner, run_id, token),
            max_model_turns=2, max_tool_calls=1,
        )

    with pytest.raises(HarnessError, match="retrieval_policy_exceeded"):
        asyncio.run(loop(False).run("reviewer", model, {"fixed": True}))
    assert asyncio.run(loop(True).run("reviewer", model, {"fixed": True})) == {"done": True}
    before = store.events(owner, run_id)
    assert asyncio.run(loop(True).run("reviewer", model, {"fixed": True})) == {"done": True}
    assert store.events(owner, run_id) == before
    assert len(model_calls) == 2 and len(tool_calls) == 1


def test_tool_repair_exhaustion_is_reconstructed_without_new_calls_after_restart(pg_store):
    store, owner, run_id, token = create_run(pg_store, policy=BudgetPolicy())
    calls = {"model": 0, "tool": 0}

    class RejectedTools:
        def retrieval_constraints(self, role):
            return {"additional_top_k": 3, "max_query_chars": 120,
                    "remaining_rounds": 2, "remaining_snippets": 6}

        def execute(self, *_):
            calls["tool"] += 1
            raise HarnessError("retrieval_policy_exceeded")

    async def model(context):
        calls["model"] += 1
        return {"tool_calls": [{"call_id": f"denied-{len(context['tool_results'])}",
                               "name": "search_knowledge",
                               "arguments": {"query": "合成", "top_k": 5}}]}

    def execute():
        loop = RoleLoop(
            RejectedTools(), lambda *_: None, before_call=lambda: None,
            journal=ExecutionJournal(store, owner, run_id, token),
        )
        with pytest.raises(HarnessError, match="invalid_role_response"):
            asyncio.run(loop.run("reviewer", model, {"fixed": True}))

    execute()
    before = store.events(owner, run_id)
    assert calls == {"model": 3, "tool": 3}
    execute()
    assert calls == {"model": 3, "tool": 3}
    assert store.events(owner, run_id) == before
