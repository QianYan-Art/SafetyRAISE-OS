from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.core.model_requests import omit_explicit_token_limits
from app.core.settings import ReportModelSettings
from app.providers.llm.openai_report import OpenAIReportProvider
from app.providers.llm.openai_vision import OpenAIVisionProvider
from app.report_harness.transport_roles import RoleModel, TransportRoles


@pytest.mark.parametrize("provider_type,model,effort", [
    (OpenAIReportProvider, "tencent/hy4-preview", "high"),
    (OpenAIVisionProvider, "openai/gpt-5.6-luna", "max"),
])
def test_model_payload_omits_output_caps_and_keeps_effort(provider_type, model, effort):
    config = ReportModelSettings.model_validate({
        "provider": "raise", "max_tokens": 16384,
        "endpoints": [{
            "name": "test", "url": "https://example.invalid/v1/chat/completions",
            "model": model, "reasoning": {"effort": effort, "max_tokens": 1000},
            "extra_body": {"max_tokens": 16, "max_completion_tokens": 32, "max_output_tokens": 64},
        }],
    })
    original = deepcopy(config.model_dump())
    provider = provider_type(config, {"test": "synthetic-key"})
    try:
        endpoint = config.endpoints[0]
        if provider_type is OpenAIReportProvider:
            payload = provider._build_payload(endpoint, model, "合成系统提示", "合成输入", [])
        else:
            payload = provider._build_payload(endpoint, model, "合成系统提示", [])
        assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & payload.keys()
        assert payload["reasoning"] == {"effort": effort}
        assert config.model_dump() == original
    finally:
        provider.close()


def test_extra_reasoning_budget_is_removed_without_mutating_configuration():
    options = {"effort": "high", "max_tokens": 8, "exclude": True}
    payload = {"reasoning": options, "max_tokens": 16}
    omit_explicit_token_limits(payload)
    assert payload == {"reasoning": {"effort": "high", "exclude": True}}
    assert options["max_tokens"] == 8


def test_reasoning_only_token_option_remains_compatible_without_effort():
    payload = {"reasoning": {"max_tokens": 8}}
    omit_explicit_token_limits(payload)
    assert payload == {"reasoning": {"max_tokens": 8}}


def test_top_level_effort_also_removes_nested_reasoning_budget():
    options = {"max_tokens": 8, "exclude": True}
    payload = {"reasoning_effort": "high", "reasoning": options}
    omit_explicit_token_limits(payload)
    assert payload == {"reasoning_effort": "high", "reasoning": {"exclude": True}}
    assert options == {"max_tokens": 8, "exclude": True}


@pytest.mark.parametrize("filename", ["workflow.yaml", "workflow.server.yaml"])
def test_default_model_profiles_use_highest_confirmed_effort_without_token_caps(filename):
    path = Path(__file__).resolve().parents[2] / "config" / filename
    models = yaml.safe_load(path.read_text("utf-8"))["models"]
    for capability, model, effort in [
        ("report_external", "tencent/hy4-preview", "high"),
        ("accident_vision", "openai/gpt-5.6-luna", "max"),
    ]:
        profile = models[capability]
        assert "max_tokens" not in profile
        assert profile["endpoints"][0]["model"] == model
        assert profile["endpoints"][0]["reasoning"] == {"effort": effort}


def test_harness_default_payload_does_not_emit_accounting_bound():
    transport = SimpleNamespace(output_limit=64000)
    roles = TransportRoles(transport, {
        role: RoleModel("synthetic") for role in ("generator", "reviewer")
    })
    for role in ("generator", "reviewer"):
        profile, payload = roles._payload(role, {"instructions": "仅合成", "facts": []})
        assert profile.output_limit_field is None
        assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & payload.keys()
