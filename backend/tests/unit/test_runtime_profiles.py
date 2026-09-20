import asyncio
from decimal import Decimal
import json

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.runtime_profiles import (
    ConfiguredAttemptClient, capacity_from_expert_metadata, capacity_from_metadata,
    price_upper_cny, openrouter_price_filter,
    capacity_budget, ModelCapacity,
)
from app.report_harness.transport import HTTPAttemptClient
from app.report_harness.contracts import canonical_digest
from app.schemas.report_run import BudgetPolicy


def metadata():
    return {
        "id": "synthetic", "context_length": 1000,
        "top_provider": {"max_completion_tokens": 100},
        "reasoning": {"supported_efforts": ["high", "low", "none"]},
        "pricing": {"prompt": "0.000001", "completion": "0.000003"},
    }


def test_capacity_is_accounting_proof_not_output_parameter():
    capacity = capacity_from_metadata(metadata(), model="synthetic", effort="high")
    assert capacity.bound().total_tokens == 1100
    assert capacity.bound().output_tokens == 100
    assert price_upper_cny(metadata(), capacity, usd_to_cny=Decimal("10")) == Decimal("0.013")


def test_wrong_model_or_nonhighest_effort_is_rejected():
    for model, effort in [("another", "high"), ("synthetic", "low")]:
        with pytest.raises(HarnessError):
            capacity_from_metadata(metadata(), model=model, effort=effort)


def test_highest_effort_does_not_depend_on_provider_list_order():
    data = metadata()
    data["reasoning"]["supported_efforts"] = ["low", "max", "high"]
    assert capacity_from_metadata(data, model="synthetic", effort="max").effort == "max"
    with pytest.raises(HarnessError, match="model_effort_not_highest"):
        capacity_from_metadata(data, model="synthetic", effort="low")
    data["reasoning"]["supported_efforts"] = ["high", "unverified-new-level"]
    with pytest.raises(HarnessError, match="model_effort_not_highest"):
        capacity_from_metadata(data, model="synthetic", effort="high")


def test_unknown_capacity_and_new_billable_category_fail_closed():
    data = metadata()
    del data["top_provider"]
    with pytest.raises(HarnessError, match="model_capacity_unverified"):
        capacity_from_metadata(data, model="synthetic", effort="high")
    capacity = capacity_from_metadata(metadata(), model="synthetic", effort="high")
    data = metadata()
    data["pricing"]["new_charge"] = "0.01"
    with pytest.raises(HarnessError, match="pricing_unverified"):
        price_upper_cny(data, capacity, usd_to_cny=Decimal("10"))


def test_expert_metadata_preserves_uncontrolled_reasoning_mode():
    capacity = capacity_from_expert_metadata(
        {"key": "synthetic", "max_context_length": 262144}, model="synthetic",
    )
    assert capacity.effort is None
    assert capacity.output_tokens == 262144


def test_configured_client_sets_effort_and_rejects_model_overrides(monkeypatch):
    sent = []

    async def attempt(self, role, payload, timeout):
        sent.append(payload)
        return {}

    monkeypatch.setattr(HTTPAttemptClient, "attempt", attempt)

    async def run():
        capacity = capacity_from_metadata(metadata(), model="synthetic", effort="high")
        client = ConfiguredAttemptClient(
            {"generator": "https://example.invalid/v1/chat/completions"}, {},
            {"generator": capacity},
            request_options={"generator": {"temperature": 0.5, "verbosity": "low"}},
        )
        try:
            await client.attempt("generator", {"model": "synthetic", "messages": []}, 1)
            assert sent[0]["reasoning"] == {"effort": "high", "exclude": True}
            assert sent[0]["temperature"] == 0.5
            assert sent[0]["verbosity"] == "low"
            assert "max_tokens" not in sent[0]
            for extra in ({"max_tokens": 100}, {"reasoning": {"effort": "none"}}):
                with pytest.raises(HarnessError, match="runtime_payload_unapproved"):
                    await client.attempt("generator", {
                        "model": "synthetic", "messages": [], **extra,
                    }, 1)
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("options", [
    {"max_tokens": 1}, {"reasoning": {"effort": "low"}},
    {"temperature": True}, {"temperature": float("nan")}, {"verbosity": "unknown"},
])
def test_server_options_cannot_reintroduce_caps_or_replace_effort(options):
    capacity = capacity_from_metadata(metadata(), model="synthetic", effort="high")
    with pytest.raises(ValueError):
        ConfiguredAttemptClient(
            {"generator": "https://example.invalid/v1/chat/completions"}, {},
            {"generator": capacity}, request_options={"generator": options},
        )


def test_price_filter_uses_per_million_units_without_output_caps():
    capacity = capacity_from_metadata(metadata(), model="synthetic", effort="high")
    routing = openrouter_price_filter(metadata(), capacity)
    assert routing["allow_fallbacks"] is False
    assert Decimal(routing["max_price"]["prompt"]) == 1
    assert Decimal(routing["max_price"]["completion"]) == 3
    assert Decimal(routing["max_price"]["request"]) == 0
    assert "max_tokens" not in routing


def test_expert_thoughts_are_removed_before_returning_response_to_ledger(monkeypatch):
    async def attempt(self, role, payload, timeout):
        return {
            "choices": [{"message": {
                "content": '<think>不应持久保存的思考</think>{"建议":"合成指导"}',
                "reasoning": "内部思考", "reasoning_content": "内部思考",
                "reasoning_details": [{"text": "内部思考"}],
            }}],
            "usage": {"total_tokens": 25},
        }

    monkeypatch.setattr(HTTPAttemptClient, "attempt", attempt)

    async def run():
        capacity = capacity_from_expert_metadata(
            {"id": "synthetic", "max_context_length": 100}, model="synthetic",
        )
        client = ConfiguredAttemptClient(
            {"expert": "https://example.invalid/chat"}, {}, {"expert": capacity},
        )
        try:
            response = await client.attempt("expert", {"model": "synthetic", "messages": []}, 1)
            assert response["choices"][0]["message"] == {"content": '{"建议":"合成指导"}'}
            assert response["usage"] == {"total_tokens": 25}
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("role", ["generator", "reviewer"])
def test_report_roles_reject_inner_json_extraction_and_preserve_usage(monkeypatch, role):
    async def attempt(self, active_role, payload, timeout):
        return {
            "choices": [{"message": {
                "content": '{"outer":{"claim":"C8"} trailing',
                "reasoning": "内部思考",
            }}],
            "usage": {"total_tokens": 31},
        }

    monkeypatch.setattr(HTTPAttemptClient, "attempt", attempt)

    async def run():
        capacity = capacity_from_metadata(metadata(), model="synthetic", effort="high")
        client = ConfiguredAttemptClient(
            {role: "https://example.invalid/chat"}, {}, {role: capacity},
        )
        try:
            response = await client.attempt(
                role, {"model": "synthetic", "messages": []}, 1,
            )
            message = response["choices"][0]["message"]
            assert message["content"] == ""
            assert "reasoning" not in message
            assert response["usage"] == {"total_tokens": 31}
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("role", ["generator", "reviewer"])
def test_report_roles_accept_only_complete_json_fence_before_redaction(monkeypatch, role):
    async def attempt(self, active_role, payload, timeout):
        return {
            "choices": [{"message": {
                "content": '```json\n{"reasoning":"内部思考","claim":"C8"}\n```',
            }}],
            "usage": {"total_tokens": 32},
        }

    monkeypatch.setattr(HTTPAttemptClient, "attempt", attempt)

    async def run():
        capacity = capacity_from_metadata(metadata(), model="synthetic", effort="high")
        client = ConfiguredAttemptClient(
            {role: "https://example.invalid/chat"}, {}, {role: capacity},
        )
        try:
            response = await client.attempt(
                role, {"model": "synthetic", "messages": []}, 1,
            )
            assert json.loads(response["choices"][0]["message"]["content"]) == {"claim": "C8"}
            assert response["usage"] == {"total_tokens": 32}
        finally:
            await client.close()

    asyncio.run(run())


def test_real_capacity_defaults_are_accounting_only_and_explicit_small_budget_is_rejected():
    capacities = {
        "expert": ModelCapacity("expert", 262144, 262144, canonical_digest("expert")),
        "generator": ModelCapacity("report", 1048576, 64000, canonical_digest("report"), "high"),
        "reviewer": ModelCapacity("report", 1048576, 64000, canonical_digest("reviewer"), "high"),
        "embedding": ModelCapacity("embedding", 32768, 0, canonical_digest("embedding")),
    }
    policy = capacity_budget(capacities)
    assert policy.max_output_tokens_per_request == 262144
    assert policy.max_total_tokens >= sum(item.bound().total_tokens for item in capacities.values())
    assert policy.max_physical_requests == BudgetPolicy().max_physical_requests
    assert policy.max_money is None
    with pytest.raises(HarnessError, match="business_token_budget_insufficient"):
        capacity_budget(capacities, BudgetPolicy())


@pytest.mark.parametrize("requests", [4, 24, 25])
def test_explicit_budget_covers_every_configured_physical_request(requests):
    capacities = {
        role: ModelCapacity(role, 100, 10, canonical_digest(role))
        for role in ("expert", "generator", "reviewer", "embedding")
    }
    configured = BudgetPolicy(
        max_physical_requests=requests, max_retrieval_requests=4,
        max_total_tokens=requests * 110, max_output_tokens_per_request=10,
    )
    assert capacity_budget(capacities, configured) == configured
    with pytest.raises(HarnessError, match="business_token_budget_insufficient"):
        capacity_budget(capacities, configured.model_copy(update={
            "max_total_tokens": requests * 110 - 1,
        }))
    if requests > 4:
        with pytest.raises(HarnessError, match="business_token_budget_insufficient"):
            capacity_budget(capacities, configured.model_copy(update={"max_total_tokens": 440}))


@pytest.mark.parametrize("role", ["expert", "generator", "reviewer"])
def test_protocol_json_redacts_thoughts_but_preserves_domain_reasoning(monkeypatch, role):
    async def attempt(self, active_role, payload, timeout):
        return {
            "choices": [{"message": {"content": json.dumps({
                "reasoning": "不应持久化", "nested": {"think": "不应持久化"},
                "report_markdown": "<think>不应持久化</think>合成结论",
                "分析依据": "保留必要业务分析", "completed_checks": [{"category": "reasoning"}],
            }, ensure_ascii=False)}}],
            "usage": {"total_tokens": 25},
        }

    monkeypatch.setattr(HTTPAttemptClient, "attempt", attempt)

    async def run():
        capacity = capacity_from_metadata(metadata(), model="synthetic", effort="high")
        client = ConfiguredAttemptClient(
            {role: "https://example.invalid/chat"}, {}, {role: capacity},
        )
        try:
            response = await client.attempt(role, {"model": "synthetic", "messages": []}, 1)
            result = json.loads(response["choices"][0]["message"]["content"])
            assert "reasoning" not in result
            assert result["nested"] == {}
            assert result["report_markdown"] == "合成结论"
            assert result["分析依据"] == "保留必要业务分析"
            assert result["completed_checks"] == [{"category": "reasoning"}]
            assert response["usage"]["total_tokens"] == 25
        finally:
            await client.close()

    asyncio.run(run())


def test_role_timeout_limits_total_attempt_without_changing_payload(monkeypatch):
    observed = []

    async def attempt(self, role, payload, timeout):
        observed.append((timeout, payload))
        await asyncio.sleep(0.1)
        return {}

    monkeypatch.setattr(HTTPAttemptClient, "attempt", attempt)

    async def run():
        capacity = capacity_from_metadata(metadata(), model="synthetic", effort="high")
        client = ConfiguredAttemptClient(
            {"generator": "https://example.invalid/chat"}, {}, {"generator": capacity},
            role_timeouts={"generator": 0.01},
        )
        try:
            with pytest.raises(TimeoutError):
                await client.attempt("generator", {"model": "synthetic", "messages": []}, 10)
            assert observed[0][0] == 0.01
            assert "max_tokens" not in observed[0][1]
        finally:
            await client.close()

    asyncio.run(run())
