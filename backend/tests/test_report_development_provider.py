from copy import deepcopy
from decimal import Decimal
import asyncio
import httpx

import pytest

from evals.report_harness.development_provider import (
    CONTEXT_LIMIT, MODEL, MODEL_OUTPUT_LIMIT, REQUEST_CNY_UPPER,
    USD_TO_CNY_UPPER, DevelopmentClient, validate_metadata,
)
from app.report_harness.errors import HarnessError
from app.report_harness.transport import HTTPAttemptClient


def metadata():
    return {"id": MODEL, "endpoints": [{
        "tag": "tencent/fp8", "context_length": CONTEXT_LIMIT,
        "max_completion_tokens": MODEL_OUTPUT_LIMIT,
        "supported_parameters": ["max_tokens", "response_format", "reasoning"],
        "pricing": {"prompt": "0.000000834", "completion": "0.000002501",
                    "input_cache_read": "0.000000042", "discount": 0},
    }]}


def test_capacity_price_bound_is_conservative():
    data = metadata()
    assert validate_metadata(data) == data["endpoints"][0]
    assert (CONTEXT_LIMIT * Decimal(".000002")
            + MODEL_OUTPUT_LIMIT * Decimal(".000003")) * USD_TO_CNY_UPPER < REQUEST_CNY_UPPER


@pytest.mark.parametrize("key,value", [
    ("prompt", "0.1"), ("completion", "-1"), ("completion", "NaN"),
    ("request", "0.01"), ("web_search", "1"),
])
def test_unbounded_prices_are_rejected(key, value):
    data = metadata()
    data["endpoints"][0]["pricing"][key] = value
    with pytest.raises(ValueError):
        validate_metadata(data)


def test_changed_capacity_and_provider_are_rejected():
    for key, value in [("context_length", CONTEXT_LIMIT * 2), ("tag", "other")]:
        data = deepcopy(metadata())
        data["endpoints"][0][key] = value
        with pytest.raises(ValueError):
            validate_metadata(data)


def test_fixed_routing_and_reasoning_redaction(monkeypatch):
    sent = []

    async def attempt(self, role, payload, timeout):
        sent.append(payload)
        return {"model": MODEL, "provider": "Tencent", "choices": [{"message": {
            "content": "{}", "reasoning": "不可保存", "reasoning_details": ["不可保存"],
        }}]}

    monkeypatch.setattr(HTTPAttemptClient, "attempt", attempt)

    async def check():
        client = DevelopmentClient("synthetic-key")
        try:
            result = await client.attempt("generator", {
                "model": MODEL, "messages": [],
            }, 1)
            assert result["choices"][0]["message"] == {"content": "{}"}
            assert sent[0]["provider"]["allow_fallbacks"] is False
            assert sent[0]["provider"]["only"] == ["tencent/fp8"]
            assert sent[0]["provider"]["max_price"]["request"] == 0
            assert sent[0]["reasoning"]["exclude"] is True
            assert sent[0]["reasoning"]["effort"] == "high"
            assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & sent[0].keys()
            assert "max_tokens" not in sent[0]["reasoning"]
            with pytest.raises(HarnessError):
                await client.attempt("generator", {
                    "model": MODEL, "messages": [],
                    "plugins": [{"id": "web"}],
                }, 1)
            assert len(sent) == 1
        finally:
            await client.close()

    asyncio.run(check())


def test_transport_failure_records_type_not_sensitive_message(monkeypatch):
    async def attempt(self, role, payload, timeout):
        raise httpx.ReadError("合成敏感异常正文，不得输出")

    monkeypatch.setattr(HTTPAttemptClient, "attempt", attempt)

    async def check():
        client = DevelopmentClient("synthetic-key")
        try:
            with pytest.raises(httpx.ReadError):
                await client.attempt("generator", {
                    "model": MODEL, "messages": [],
                }, 1)
            assert client.last_error_type == "ReadError"
            assert client.last_http_status is None
        finally:
            await client.close()

    asyncio.run(check())


@pytest.mark.parametrize("field", ["max_tokens", "max_completion_tokens", "max_output_tokens", "reasoning"])
def test_development_client_rejects_unapproved_token_or_reasoning_override(field, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("不合法请求不应进入物理发送")

    monkeypatch.setattr(HTTPAttemptClient, "attempt", forbidden)

    async def check():
        client = DevelopmentClient("synthetic-key")
        try:
            with pytest.raises(HarnessError, match="development_payload_unapproved"):
                await client.attempt("reviewer", {"model": MODEL, "messages": [], field: 10}, 1)
        finally:
            await client.close()

    asyncio.run(check())
