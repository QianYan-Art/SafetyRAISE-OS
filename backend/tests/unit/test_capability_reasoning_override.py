"""用户能力配置中的推理等级：留空沿用服务端默认，选定后覆盖，off 时不发送该参数。"""

import pytest

from app.core.model_requests import omit_explicit_token_limits
from app.core.settings import ReportModelSettings
from app.providers.llm.openai_report import OpenAIReportProvider
from app.providers.llm.openai_vision import OpenAIVisionProvider
from app.schemas.user_config import CapabilityTuningParams
from app.services.report_service import ReportService, resolve_reasoning_override
from app.services.user_capability_config_service import UserCapabilityConfigService


def _model_config(model: str, effort: str) -> ReportModelSettings:
    return ReportModelSettings.model_validate({
        "provider": "raise",
        "endpoints": [{
            "name": "system", "url": "https://example.invalid/v1/chat/completions",
            "model": model, "reasoning": {"effort": effort},
        }],
    })


def _payload_reasoning(provider_type, config, model):
    provider = provider_type(config, {config.endpoints[0].name: "synthetic-key"})
    try:
        endpoint = config.endpoints[0]
        if provider_type is OpenAIReportProvider:
            payload = provider._build_payload(endpoint, model, "合成系统提示", "合成输入", [])
        else:
            payload = provider._build_payload(endpoint, model, "合成系统提示", [])
        omit_explicit_token_limits(payload)
        return {key: payload.get(key) for key in ("reasoning", "reasoning_effort") if key in payload}
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


def test_resolve_reasoning_override_branches():
    assert resolve_reasoning_override(None) == {}
    assert resolve_reasoning_override({}) == {}
    assert resolve_reasoning_override({"params": {}}) == {}
    assert resolve_reasoning_override({"params": {"reasoning_effort": None}}) == {}

    off = resolve_reasoning_override({"params": {"reasoning_effort": "off"}})
    assert off == {"reasoning": None, "reasoning_effort": None}

    picked = resolve_reasoning_override({"params": {"reasoning_effort": "low"}})
    assert picked["reasoning"].effort == "low"
    # 端点校验不允许同时存在两种写法。
    assert picked["reasoning_effort"] is None


@pytest.mark.parametrize("provider_type,model,system_effort", [
    (OpenAIVisionProvider, "openai/gpt-5.6-luna", "max"),
    (OpenAIReportProvider, "tencent/hy4-preview", "high"),
])
def test_user_override_keeps_system_effort_when_not_chosen(provider_type, model, system_effort):
    config = _model_config(model, system_effort)
    new_config, _ = ReportService._synthesize_override_endpoint(
        config, {"base_url": "https://user.invalid/v1", "model_name": "user-model", "params": {}}, name="user"
    )
    assert new_config.endpoints[0].model == "user-model"
    assert _payload_reasoning(provider_type, new_config, "user-model") == {"reasoning": {"effort": system_effort}}


@pytest.mark.parametrize("provider_type,model", [
    (OpenAIVisionProvider, "openai/gpt-5.6-luna"),
    (OpenAIReportProvider, "tencent/hy4-preview"),
])
def test_user_override_applies_chosen_effort(provider_type, model):
    config = _model_config(model, "max")
    new_config, _ = ReportService._synthesize_override_endpoint(
        config,
        {"base_url": "https://user.invalid/v1", "model_name": "user-model", "params": {"reasoning_effort": "low"}},
        name="user",
    )
    assert _payload_reasoning(provider_type, new_config, "user-model") == {"reasoning": {"effort": "low"}}


@pytest.mark.parametrize("provider_type,model", [
    (OpenAIVisionProvider, "openai/gpt-5.6-luna"),
    (OpenAIReportProvider, "tencent/hy4-preview"),
])
def test_off_omits_reasoning_for_upstreams_without_support(provider_type, model):
    config = _model_config(model, "max")
    new_config, _ = ReportService._synthesize_override_endpoint(
        config,
        {"base_url": "https://user.invalid/v1", "model_name": "user-model", "params": {"reasoning_effort": "off"}},
        name="user",
    )
    assert _payload_reasoning(provider_type, new_config, "user-model") == {}


def test_normalize_params_scopes_effort_to_vision_and_report():
    normalize = UserCapabilityConfigService._normalize_params
    incoming = CapabilityTuningParams(reasoning_effort="high")

    for capability in ("vision", "report"):
        assert normalize(capability, incoming, None) == {"reasoning_effort": "high"}
        # 提交了 params 即以本次为准，显式清空回到服务端默认。
        assert normalize(capability, CapabilityTuningParams(), {"reasoning_effort": "max"}) == {}
        # 未提交 params 时保留原值。
        assert normalize(capability, None, {"reasoning_effort": "max"}) == {"reasoning_effort": "max"}

    # 嵌入用途只接受检索参数，不接受推理等级。
    assert normalize("embedding", incoming, None) == {}
    assert normalize("embedding", CapabilityTuningParams(top_k=5), None) == {"top_k": 5}
