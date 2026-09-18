import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from app.api.routes_report_runs import get_report_run_service
from app.main import app
from app.report_harness.contracts import canonical_digest
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.transport import BudgetedTransport, HTTPAttemptClient, RequestBound
from app.report_harness.transport_roles import RoleModel, TransportRoles
from app.schemas.report_run import BudgetPolicy
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles, dependencies
from tests.test_budgeted_transport import physical_server
from tests.test_report_tool_policy import tool_client


def model_reply(role, *, usage=17):
    def reply(payload):
        context = json.loads(payload["messages"][1]["content"])
        if role == "expert":
            result = {"guidance": {"note": "仅合成工程验证"}}
        else:
            roles = SyntheticRoles()
            method = roles.generate if role == "generator" else roles.review
            result = asyncio.run(method(context))
        response = {"choices": [{
            "finish_reason": "stop", "message": {"content": json.dumps(result, ensure_ascii=False)},
        }]}
        if usage is not None:
            response["usage"] = {"total_tokens": usage}
        return response
    return reply


def runtime_factory(url, config, *, expert=False):
    def factory(store, owner, run_id, token):
        proof = canonical_digest({"protocol": "fixed-synthetic-http-usage"})
        registered = ["generator", "reviewer"] + (["expert"] if expert else [])
        transport = BudgetedTransport(
            RequestLedger(store), HTTPAttemptClient({role: url for role in registered}),
            owner=owner, run_id=run_id, token=token,
            endpoint_digest=config.endpoint_profile_digest,
            output_limit=20, generation_reserve_tokens=200, review_reserve_tokens=200,
            authorize=lambda: store.assert_active(owner, run_id, token),
            remaining_seconds=lambda: 30 - store.get(owner, run_id)["active_seconds"],
            bound_provider=lambda role, payload: RequestBound(200, payload["max_tokens"], proof),
            verified_proofs=frozenset({proof}),
        )
        return TransportRoles(
            transport, {role: RoleModel("synthetic-" + role, output_limit_field="max_tokens")
                        for role in registered},
        )
    return factory


def execute_http(fixture, url, *, policy=None, expert=False):
    client, store, owner, session = fixture
    config = replace(dependencies(SyntheticRoles()), budget_policy=policy or BudgetPolicy())
    config = replace(config, runtime_roles_factory=runtime_factory(url, config, expert=expert))
    service = ReportRunService(store, config)
    app.dependency_overrides[get_report_run_service] = lambda: service
    response = client.post("/api/v1/report-runs", json={
        "request_id": str(uuid4()), "session_id": session,
        "accident_data": {"事实": "合成的物理 HTTP 验证"}, "evidence_revision": 0,
    })
    assert response.status_code == 201
    run_id = response.json()["run_id"]
    response = client.post(f"/api/v1/report-runs/{run_id}/execute/stream",
                           json={"expected_version": 0})
    assert response.status_code == 200
    result = client.get(f"/api/v1/report-runs/{run_id}").json()
    return result, client.get(f"/api/v1/report-runs/{run_id}/events").json()


def test_authenticated_run_uses_real_http_gateway_for_each_role(tool_client):
    with physical_server([
        (200, model_reply("expert")), (200, model_reply("generator")), (200, model_reply("reviewer")),
    ]) as (url, calls):
        result, events = execute_http(tool_client, url, expert=True)
        assert result["state"] == "published", result["terminal_reason"]
        assert [call["model"] for call in calls] == [
            "synthetic-expert", "synthetic-generator", "synthetic-reviewer",
        ]
    assert result["budget"]["physical_requests"] == 3
    assert result["budget"]["known_used"] == 51
    assert result["budget"]["remaining"] == 120000 - 51
    assert result["budget"]["active_seconds"] > 0
    assert not result["formal_export_eligible"]
    assert len([event for event in events["events"] if event["type"] == "request"]) == 6


def test_frozen_policy_caps_actual_http_output_limit(tool_client):
    with physical_server([
        (200, model_reply("generator")), (200, model_reply("reviewer")),
    ]) as (url, calls):
        result, _ = execute_http(
            tool_client, url, policy=BudgetPolicy(max_output_tokens_per_request=7),
        )
        assert result["state"] == "published", result["terminal_reason"]
        assert [call["max_tokens"] for call in calls] == [7, 7]


@pytest.mark.parametrize("usage,state,reason", [
    (None, "suspended", "usage_unknown"), (201, "needs_review", "usage_exceeded"),
])
def test_unknown_or_excess_usage_blocks_following_roles(tool_client, usage, state, reason):
    with physical_server([(200, model_reply("generator", usage=usage))]) as (url, calls):
        result, _ = execute_http(tool_client, url)
        assert len(calls) == 1
    assert result["state"] == state, result["terminal_reason"]
    assert result["terminal_reason"] == reason
    assert result["budget"]["physical_requests"] == 1
    if usage is None:
        assert result["budget"]["unknown_reserved"] == 200
        assert result["budget"]["known_used"] == 0
    else:
        assert result["budget"]["known_used"] == 201
    assert "report" not in result


@pytest.mark.parametrize("expert,request_limit,token_limit", [
    (False, 1, 120000), (False, 24, 399), (True, 2, 120000), (True, 24, 599),
])
def test_initial_budget_keeps_generation_and_final_review_before_spending(
    tool_client, expert, request_limit, token_limit,
):
    with physical_server([]) as (url, calls):
        result, _ = execute_http(tool_client, url, expert=expert, policy=BudgetPolicy(
            max_physical_requests=request_limit, max_retrieval_requests=0,
            max_total_tokens=token_limit,
        ))
        assert calls == []
    assert result["state"] == "needs_review", result["terminal_reason"]
    assert result["terminal_reason"] == "budget_exhausted"
    assert result["budget"]["physical_requests"] == 0
