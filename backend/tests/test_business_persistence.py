"""真实 PostgreSQL 上的业务控制器回归；角色响应为合成数据，不证明报告质量。"""

import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from app.report_harness.business_workflow import BusinessWorkflow
from app.report_harness.contracts import canonical_digest
from app.report_harness.controlled_tools import ControlledTools
from app.report_harness.errors import HarnessError
from app.report_harness.journal import ExecutionJournal
from app.report_harness.role_loop import RoleLoop
from app.schemas.report_run import CreateRunRequest
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles, dependencies


def business_service(pg_store, roles=None):
    store, owner, _, session = pg_store
    roles = roles or SyntheticRoles()
    base = dependencies(roles)
    text = "合成左转碰撞：仅用于工程验证的驾驶规则。"
    source = {
        "id": "synthetic-rule", "document_id": "synthetic-doc", "version": "1",
        "text": text, "digest": canonical_digest(text),
        "manifest_digest": base.knowledge_manifest_digest,
    }
    runtime = replace(
        base, business_workflow=BusinessWorkflow("合成专家模板", "合成报告模板"),
        knowledge_chunks=(source,), external_knowledge_source=True, max_active_runs=1,
    )
    service = ReportRunService(store, runtime)
    run = service.create(owner, CreateRunRequest(
        request_id=uuid4(), session_id=session, evidence_revision=0,
        accident_data={"事故经过": "合成左转碰撞", "天气": "晴天"},
    ))
    return service, runtime, roles, run


def test_initial_retrieval_replays_from_postgres_without_repeating_operation(pg_store):
    store, owner, _, _ = pg_store
    service, runtime, _, run = business_service(pg_store)
    run_id = run["run_id"]
    token = service.claim(owner, run_id, 0)
    record = store.get(owner, run_id)

    def make_tools():
        return ControlledTools(
            record["snapshot"], list(runtime.knowledge_chunks),
            retrieval_policy=runtime.business_workflow.retrieval_policy(),
        )

    first_tools = make_tools()
    first = RoleLoop(
        first_tools, lambda *_: None, before_call=lambda: None,
        journal=ExecutionJournal(store, owner, run_id, token),
    )
    result = asyncio.run(first.initial_retrieval("合成左转碰撞", 3))
    restored = make_tools()
    restored.initial_retrieval = lambda *_: pytest.fail("恢复不得重新执行已完成首检")
    replay = RoleLoop(
        restored, lambda *_: None, before_call=lambda: None,
        journal=ExecutionJournal(store, owner, run_id, token),
    )
    assert asyncio.run(replay.initial_retrieval("合成左转碰撞", 3)) == result
    persisted = store.get(owner, run_id)
    assert persisted["execution_journal"]["attempts"]["tool"] == 1
    assert restored.accessed_knowledge("generator") == {"synthetic-rule"}
    assert restored.accessed_knowledge("reviewer") == set()


def test_business_flow_persists_used_sources_without_copying_corpus(pg_store):
    store, owner, _, _ = pg_store
    service, runtime, roles, run = business_service(pg_store)
    assert store.get(owner, run["run_id"])["knowledge_source"] == []
    result = asyncio.run(service.execute(owner, run["run_id"], 0))
    assert result["state"] == "published"
    record = store.get(owner, run["run_id"])
    assert record["knowledge_registry"] == list(runtime.knowledge_chunks)
    assert record["business_prompts"] == runtime.business_workflow.prompts()
    assert roles.calls == ["prepare", "generate", "review"]
    assert "prepared" not in roles.review_context
    assert "initial_knowledge_snippets" not in roles.review_context


def test_changed_business_template_cannot_resume_persisted_input(pg_store):
    store, owner, _, _ = pg_store
    _, runtime, roles, run = business_service(pg_store)
    changed = ReportRunService(store, replace(
        runtime, business_workflow=replace(runtime.business_workflow, report_prompt="新模板"),
    ))
    with pytest.raises(HarnessError, match="authorization_stale"):
        asyncio.run(changed.execute(owner, run["run_id"], 0))
    assert roles.calls == []


def test_global_capacity_applies_across_service_instances(pg_store):
    store, owner, _, _ = pg_store
    first, runtime, _, run = business_service(pg_store)
    first.claim(owner, run["run_id"], 0)
    second = ReportRunService(store, runtime)
    session = "capacity-" + str(uuid4())
    with store.connection() as conn:
        conn.execute("INSERT INTO chat_sessions(id,owner_user_id) VALUES (%s,%s)",
                     (session, owner))
    try:
        pending = second.create(owner, CreateRunRequest(
            request_id=uuid4(), session_id=session, evidence_revision=0,
            accident_data={"事故经过": "另一合成事故"},
        ))
        with pytest.raises(HarnessError, match="server_busy"):
            second.claim(owner, pending["run_id"], 0)
        assert store.get(owner, pending["run_id"])["state"] == "queued"
    finally:
        with store.connection() as conn:
            conn.execute("DELETE FROM report_runs WHERE session_id=%s", (session,))
            conn.execute("DELETE FROM chat_sessions WHERE id=%s", (session,))
