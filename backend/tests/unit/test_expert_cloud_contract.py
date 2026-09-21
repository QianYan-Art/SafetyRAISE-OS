from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from app.core.exceptions import InputValidationError, WorkflowError
from app.core.settings import ModelEndpointSettings, load_settings
from app.providers.llm.openai_compatible_expert import OpenAICompatibleExpertProvider
from app.report_harness.authorization import (
    AuthorizationCatalog,
    EndpointDescription,
    KnowledgeCollection,
)
from app.report_harness.contracts import canonical_digest
from app.schemas.user_config import UpdateCapabilityConfigsRequest
from app.services.readiness_service import ReadinessService
from app.services.report_run_service import ReportRunService
from app.services.user_capability_config_service import CAPABILITIES
from app.workflow.nodes import WorkflowNodes
from pydantic import ValidationError

SERVER_CONFIG = Path(__file__).parents[2] / "config" / "workflow.server.yaml"


class _Response:
    status_code = 200

    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _SequenceClient:
    def __init__(self, outcomes: list[Exception | _Response]):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def post(self, url: str, *, headers: dict, json: dict):
        self.calls.append({"url": url, "headers": headers, "json": json})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        return None


def _expert_provider(client: _SequenceClient) -> OpenAICompatibleExpertProvider:
    provider = OpenAICompatibleExpertProvider(
        ModelEndpointSettings(
            provider="openai_compatible",
            model="suyuan37/SafetyRAISE-TS-Qwen3",
            base_url="https://expert.invalid/v1",
            api_key_env="MODAL_EXPERT_PROXY_TOKEN",
            timeout_seconds=1800,
            prewarm_enabled=False,
        ),
        api_key="proxy-id.proxy-secret",
    )
    provider._client.close()
    provider._client = client
    return provider


def _retry_nodes() -> WorkflowNodes:
    nodes = object.__new__(WorkflowNodes)
    nodes.settings = SimpleNamespace(
        workflow=SimpleNamespace(
            retry=SimpleNamespace(max_attempts=2, backoff_seconds=0),
        ),
    )
    nodes.cancel_event = None
    return nodes


def test_server_defaults_to_modal_without_lmstudio_or_output_limit(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "EXPERT_LOCAL_PROVIDER",
        "EXPERT_LOCAL_MODEL",
        "EXPERT_LOCAL_BASE_URL",
        "EXPERT_LOCAL_API_KEY_ENV",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(str(SERVER_CONFIG))
    expert = settings.models.expert_local

    assert expert.provider == "openai_compatible"
    assert expert.model == "suyuan37/SafetyRAISE-TS-Qwen3"
    assert expert.base_url == (
        "https://qianyan-art--safetyraise-qwen3-expert-serve.eu-west.modal.run/v1"
    )
    assert expert.api_key_env == "MODAL_EXPERT_PROXY_TOKEN"
    assert expert.timeout_seconds == 1800
    assert expert.lmstudio_ttl_seconds is None
    assert expert.max_tokens is None


def test_expert_success_uses_bearer_and_posts_once_without_output_limit():
    client = _SequenceClient([
        _Response({"choices": [{"message": {"content": '{"建议":"合成"}'}}]}),
    ])
    provider = _expert_provider(client)

    assert provider.generate("系统", "用户") == '{"建议":"合成"}'
    assert len(client.calls) == 1
    request = client.calls[0]
    assert request["url"] == "https://expert.invalid/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer proxy-id.proxy-secret"
    assert request["json"] == {
        "model": "suyuan37/SafetyRAISE-TS-Qwen3",
        "messages": [
            {"role": "system", "content": "系统"},
            {"role": "user", "content": "用户"},
        ],
    }


def test_read_timeout_is_not_retried():
    request = httpx.Request("POST", "https://expert.invalid/v1/chat/completions")
    client = _SequenceClient([httpx.ReadTimeout("仍可能在远端生成", request=request)])

    with pytest.raises(WorkflowError):
        _retry_nodes()._retry_generate(
            provider=_expert_provider(client),
            system_prompt="系统",
            user_prompt="用户",
            stage_name="guidance",
        )

    assert len(client.calls) == 1


def test_connection_failure_retries_only_after_no_result():
    request = httpx.Request("POST", "https://expert.invalid/v1/chat/completions")
    client = _SequenceClient([
        httpx.ConnectError("未建立连接", request=request),
        _Response({"choices": [{"message": {"content": '{"建议":"恢复"}'}}]}),
    ])

    result = _retry_nodes()._retry_generate(
        provider=_expert_provider(client),
        system_prompt="系统",
        user_prompt="用户",
        stage_name="guidance",
    )

    assert result == '{"建议":"恢复"}'
    assert len(client.calls) == 2


def test_invalid_guidance_json_does_not_start_a_repair_generation():
    nodes = _retry_nodes()
    nodes.expert_provider = SimpleNamespace(
        generate=lambda **_: pytest.fail("JSON 解析失败后不得再次调用专家模型"),
    )

    with pytest.raises(InputValidationError, match="JSON"):
        nodes._parse_guidance_json("不是 JSON", "提示词")


def test_remote_readiness_is_sanitized_and_does_not_wake_gpu(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("MODAL_EXPERT_PROXY_TOKEN", "proxy-id.proxy-secret")
    settings = load_settings(str(SERVER_CONFIG))
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *args, **kwargs: pytest.fail("readiness 不得探测并唤醒远端 GPU"),
    )

    result = ReadinessService(settings)._check_expert_model_endpoint(
        settings.models.expert_local,
    )

    assert result == {
        "ok": True,
        "message": "系统指导服务已配置，将在需要时按需启动。",
    }


def test_missing_proxy_token_reports_sanitized_reason(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MODAL_EXPERT_PROXY_TOKEN", raising=False)
    settings = load_settings(str(SERVER_CONFIG))

    result = ReadinessService(settings)._check_expert_model_endpoint(
        settings.models.expert_local,
    )

    assert result == {
        "ok": False,
        "message": "系统指导服务凭据不可用。",
        "reason": "api_key_unavailable",
    }


def test_authorization_preview_hides_expert_endpoint():
    endpoints = [
        EndpointDescription(
            role=role,
            label=role,
            base_url=f"https://{role}.invalid/v1",
            model=f"{role}-model",
            version="v1",
        )
        for role in ("expert", "generator", "reviewer", "embedding")
    ]
    knowledge = [
        KnowledgeCollection(
            collection_id="traffic-law",
            version="v1",
            content_digest="a" * 64,
            label="交通法规",
        ),
    ]
    knowledge_digest = canonical_digest([
        item.model_dump(mode="json") for item in knowledge
    ])
    catalog = AuthorizationCatalog(endpoints, knowledge, frozenset({knowledge_digest}))
    record = {
        "snapshot_digest": "b" * 64,
        "endpoint_profile_digest": catalog.endpoint_digest,
        "snapshot": {"knowledge_manifest_digest": catalog.knowledge_digest},
    }
    store = SimpleNamespace(get=lambda owner, run_id: record)
    dependencies = SimpleNamespace(authorization_catalog=catalog)

    preview = ReportRunService(store, dependencies).authorization_preview("owner", "run")

    assert {item["role"] for item in preview["endpoints"]} == {
        "generator",
        "reviewer",
        "embedding",
    }


def test_report_metadata_and_user_capabilities_do_not_expose_expert():
    nodes = object.__new__(WorkflowNodes)
    nodes.cancel_event = None
    nodes.progress_callback = None
    nodes.retriever = SimpleNamespace(metadata={})
    nodes.report_provider = SimpleNamespace(last_used_model="report-model")
    nodes.settings = SimpleNamespace(
        models=SimpleNamespace(
            report_external=SimpleNamespace(model="report-model"),
        ),
    )
    nodes._load_report_prompt = lambda: "模板"
    nodes._generate_report_with_agentic_rag = lambda **kwargs: (
        "提示词",
        "# 合成报告",
        [],
        [],
    )
    nodes._normalize_report_output = lambda raw: (raw, [])
    nodes._agentic_rag_enabled = lambda: False
    nodes._summarize_agentic_rounds = lambda rounds: []

    output = nodes.generate_report_node({})["report_output"]

    assert output["meta"]["report_model"] == "report-model"
    assert "expert_model" not in output["meta"]
    assert CAPABILITIES == ("vision", "embedding", "report")
    with pytest.raises(ValidationError):
        UpdateCapabilityConfigsRequest.model_validate({
            "items": [{"capability": "expert"}],
        })
