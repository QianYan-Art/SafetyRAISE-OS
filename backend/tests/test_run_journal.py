import asyncio
from uuid import uuid4

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.journal import ExecutionJournal
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
