from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.routes_report_runs import get_report_run_service
from app.core.security import create_access_token
from app.main import app
from app.report_harness.contracts import canonical_digest
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles, dependencies
from tests.test_report_run_auth import _prepare_auth_users, _synthetic_settings


@pytest.fixture
def tool_client(pg_store, monkeypatch):
    store, owner, other, session = pg_store
    import os
    from types import SimpleNamespace
    from app.report_harness.test_database import validate_test_dsn

    dsn = validate_test_dsn(os.environ["REPORT_HARNESS_TEST_DSN"])
    _prepare_auth_users(dsn, owner, other)
    settings = _synthetic_settings(dsn)
    monkeypatch.setattr(deps, "get_settings", lambda: settings)
    app.dependency_overrides[deps.get_database_service] = lambda: SimpleNamespace(
        connection=store.connection)
    token = create_access_token(auth_settings=settings.auth, user_id=owner,
                                username=owner, role="user")
    try:
        with TestClient(app) as client:
            client.headers["Authorization"] = f"Bearer {token}"
            yield client, store, owner, session
    finally:
        app.dependency_overrides.pop(deps.get_database_service, None)
        app.dependency_overrides.pop(get_report_run_service, None)
        with store.connection() as conn:
            conn.execute("DELETE FROM users WHERE username=%s",
                         (settings.auth.bootstrap_admin_username,))


class QueryRoles(SyntheticRoles):
    def __init__(self, bad_tool=None, skip_reviewer=False):
        super().__init__()
        self.bad_tool = bad_tool
        self.skip_reviewer = skip_reviewer
        self.contexts = {}

    def query(self, role, context):
        self.contexts[role] = context
        results = context["tool_results"]
        if self.bad_tool and role == "generator":
            name, arguments = self.bad_tool
        elif not results:
            name, arguments = "list_evidence", {}
        elif results[-1]["name"] == "read_evidence" and results[-1]["result"].get("next_cursor"):
            name = "read_evidence"
            arguments = {"evidence_ids": [
                ref for item in context["snapshot"]["fact_obligations"]
                for ref in item["source_refs"]
            ], "cursor": results[-1]["result"]["next_cursor"]}
        elif not any(item["name"] == "read_evidence" for item in results):
            name = "read_evidence"
            arguments = {"evidence_ids": [
                ref for item in context["snapshot"]["fact_obligations"]
                for ref in item["source_refs"]
            ]}
        elif not any(item["name"] == "search_knowledge" for item in results):
            name, arguments = "search_knowledge", {"query": "合成规则", "top_k": 1}
        elif not any(item["name"] == "read_knowledge" for item in results):
            name, arguments = "read_knowledge", {"chunk_ids": ["rule-1"]}
        else:
            return None
        return {"tool_calls": [{"call_id": f"{role}-{len(results)}",
                                "name": name, "arguments": arguments}]}

    async def generate(self, context):
        query = self.query("generator", context)
        if query:
            return query
        result = await super().generate(context)
        result["claims"][0]["knowledge_refs"] = ["rule-1"]
        return result

    async def review(self, context):
        if not self.skip_reviewer:
            query = self.query("reviewer", context)
            if query:
                return query
        result = await super().review(context)
        result["completed_checks"][0]["knowledge_refs"] = ["rule-1"]
        return result


def run_http(fixture, roles, *, text="合成样本", manifest=None):
    client, store, owner, session = fixture
    config = dependencies(roles)
    chunk = {
        "id": "rule-1", "document_id": "synthetic-rule", "version": "v1",
        "text": "合成规则仅验证工程行为。", "digest": canonical_digest("合成规则仅验证工程行为。"),
        "manifest_digest": manifest or config.knowledge_manifest_digest,
    }
    service = ReportRunService(store, replace(config, knowledge_chunks=(chunk,)))
    app.dependency_overrides[get_report_run_service] = lambda: service
    response = client.post("/api/v1/report-runs", json={
        "request_id": str(uuid4()), "session_id": session,
        "accident_data": {"事实": text}, "evidence_revision": 0,
    })
    assert response.status_code == 201
    run_id = response.json()["run_id"]
    stream = client.post(f"/api/v1/report-runs/{run_id}/execute/stream",
                         json={"expected_version": 0})
    assert stream.status_code == 200
    return store.get(owner, run_id), client.get(
        f"/api/v1/report-runs/{run_id}/events").json()


def test_http_roles_query_frozen_sources_and_persist_private_trace(tool_client):
    roles = QueryRoles()
    record, events = run_http(tool_client, roles)
    assert record["state"] == "published", record["terminal_reason"]
    completed = [item for item in record["tool_history"] if item["status"] == "completed"]
    assert len(completed) == 8
    assert {item["role"] for item in completed} == {"generator", "reviewer"}
    assert all("result" in item["private"] for item in completed)
    assert all("private" not in event["data"] for event in events["events"])
    assert "合成规则仅验证工程行为" not in str(events)
    assert all(item["call_id"].startswith("reviewer") for item in
               roles.contexts["reviewer"]["tool_results"])
    assert len(roles.contexts["generator"]["tools"]) == 4


@pytest.mark.parametrize("bad_tool", [
    ("shell", {"command": "whoami"}),
    ("read_evidence", {"evidence_ids": ["C:/private.txt"]}),
    ("read_knowledge", {"chunk_ids": ["https://outside.invalid/secret"]}),
    ("search_knowledge", {"query": "合成", "top_k": 1, "url": "https://outside.invalid"}),
])
def test_http_tool_escalation_is_denied_and_recorded(tool_client, bad_tool):
    record, events = run_http(tool_client, QueryRoles(bad_tool=bad_tool))
    assert record["state"] == "failed"
    assert record["tool_history"][-1]["status"] == "denied"
    assert any(event["data"].get("status") == "denied" for event in events["events"])
    assert "report" not in record


def test_http_review_cannot_cite_generator_only_knowledge(tool_client):
    record, _ = run_http(tool_client, QueryRoles(skip_reviewer=True))
    assert record["state"] == "needs_review"
    assert "report" not in record


def test_http_wrong_knowledge_manifest_stops_before_roles(tool_client):
    roles = QueryRoles()
    record, _ = run_http(tool_client, roles, manifest="0" * 64)
    assert record["state"] == "failed"
    assert record["terminal_reason"] == "knowledge_manifest_conflict"
    assert roles.calls == []


def test_http_catalogue_does_not_count_as_read_original(tool_client):
    record, _ = run_http(tool_client, SyntheticRoles(), text="合成" * 17000)
    assert record["state"] == "needs_review"
    assert "report" not in record


def test_http_large_original_is_read_in_pages_by_both_roles(tool_client):
    roles = QueryRoles()
    record, _ = run_http(tool_client, roles, text="合成" * 17000)
    assert record["state"] == "published", record["terminal_reason"]
    for role in ("generator", "reviewer"):
        assert roles.contexts[role]["snapshot"]["content_mode"] == "catalogue"
        pages = [item["result"] for item in roles.contexts[role]["tool_results"]
                 if item["name"] == "read_evidence"]
        assert len(pages) >= 2
        assert pages[0]["truncated"] and not pages[-1]["truncated"]


def test_http_server_knowledge_changed_after_create_requires_new_run(tool_client):
    client, store, owner, session = tool_client
    roles = SyntheticRoles()
    config = dependencies(roles)
    chunk = {
        "id": "rule-1", "document_id": "synthetic", "version": "1", "text": "合成",
        "digest": canonical_digest("合成"), "manifest_digest": config.knowledge_manifest_digest,
    }
    service = ReportRunService(store, replace(config, knowledge_chunks=(chunk,)))
    app.dependency_overrides[get_report_run_service] = lambda: service
    created = client.post("/api/v1/report-runs", json={
        "request_id": str(uuid4()), "session_id": session,
        "accident_data": {"事实": "合成"}, "evidence_revision": 0,
    })
    run_id = created.json()["run_id"]
    chunk["text"] = "变更"
    chunk["digest"] = canonical_digest("变更")
    response = client.post(f"/api/v1/report-runs/{run_id}/execute/stream",
                           json={"expected_version": 0})
    assert response.status_code == 409
    assert store.get(owner, run_id)["knowledge_source"][0]["text"] == "合成"
    assert not roles.calls
