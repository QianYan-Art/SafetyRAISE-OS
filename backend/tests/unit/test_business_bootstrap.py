from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from app.api.routes_report_runs import get_report_run_service
from app.report_harness import business_bootstrap as bootstrap
from app.report_harness.authorization import KnowledgeCollection
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.money_guard import RoleBillingContract
from app.report_harness.runtime_profiles import capacity_from_expert_metadata, capacity_from_metadata
from app.report_harness.business_server import install_business_runtime
from app.schemas.report_run import BudgetPolicy


@pytest.fixture
def configured(tmp_path, monkeypatch):
    ledger = tmp_path / "synthetic-money.sqlite3"
    ledger.touch()
    expert_prompt, report_prompt = tmp_path / "expert.md", tmp_path / "report.md"
    expert_prompt.write_text("合成专家模板", encoding="utf-8")
    report_prompt.write_text("合成报告模板", encoding="utf-8")
    report_endpoint = SimpleNamespace(
        name="primary", url="https://openrouter.ai/api/v1/chat/completions",
        model="synthetic-report", extra_body={}, reasoning=None, reasoning_effort="high",
        api_key_env=None, connection=None, temperature=None, timeout_seconds=120,
    )
    settings = SimpleNamespace(
        models=SimpleNamespace(
            expert_local=SimpleNamespace(
                model="synthetic-expert", base_url="https://expert.invalid/v1", api_key_env=None,
                temperature=None, timeout_seconds=120,
            ),
            retrieval_embedding=SimpleNamespace(
                model="synthetic-embedding", base_url="https://openrouter.ai/api/v1",
                api_key_env=None, api_key=None, dimensions=2,
                query_instruction="", query_max_length=512, timeout_seconds=120,
            ),
            report_external=SimpleNamespace(
                model="synthetic-report", endpoints=[report_endpoint], api_key_env=None,
                temperature=0.5,
            ),
        ),
        guidance_prompt_file=expert_prompt, report_prompt_file=report_prompt,
    )
    metadata = {
        "expert": {"id": "synthetic-expert", "max_context_length": 100},
        **{role: {
            "id": "synthetic-embedding" if role == "embedding" else "synthetic-report",
            "context_length": 100, "top_provider": {"max_completion_tokens": 20},
            "reasoning": {"supported_efforts": ["high", "low"]},
            "pricing": {"prompt": "0.000001", "completion": "0.000003"},
        } for role in ("generator", "reviewer", "embedding")},
    }
    capacities = {
        "expert": capacity_from_expert_metadata(metadata["expert"], model="synthetic-expert"),
        **{role: capacity_from_metadata(
            metadata[role], model=metadata[role]["id"],
            effort=None if role == "embedding" else "high", embedding=role == "embedding",
        ) for role in ("generator", "reviewer", "embedding")},
    }
    endpoints = {
        "expert": "https://expert.invalid/v1/chat/completions",
        "embedding": "https://openrouter.ai/api/v1/embeddings",
        "generator": report_endpoint.url, "reviewer": report_endpoint.url,
    }
    contracts = {role: RoleBillingContract(
        role=role, model=capacity.model,
        endpoint_digest=bootstrap.billing_endpoint_digest(role, endpoints[role], capacity),
        quote_cny=Decimal("0") if role == "expert" else Decimal("1"),
        billing_mode="local_token_free" if role == "expert" else "remote_actual",
        usage_source="合成响应usage",
    ) for role, capacity in capacities.items()}
    digest = canonical_digest("合成已登记合同")
    billing = {"contracts": contracts, "billing_contract_digest": digest,
               "usd_to_cny_upper": Decimal("10")}
    monkeypatch.setattr(bootstrap, "read_billing_contract", lambda *_: billing)
    assets = SimpleNamespace(
        collection=KnowledgeCollection(
            collection_id="synthetic", label="合成知识", version="1",
            content_digest=canonical_digest("合成知识"),
        ),
        chunks=(), retriever=lambda _: None, validate=lambda: None,
    )
    monkeypatch.setattr(bootstrap, "load_knowledge_assets", lambda *_a, **_k: assets)
    manifest = bootstrap.BusinessRuntimeManifest(
        knowledge_content_digest=assets.collection.content_digest,
        billing_contract_digest=digest, ledger_path=str(ledger),
        experiment_id="synthetic-existing-only",
        generator_endpoint_name="primary", reviewer_endpoint_name="primary",
        metadata=metadata, budget=BudgetPolicy(),
    )
    return settings, manifest, billing


def test_assembly_connects_original_templates_without_enabling_formal_release(configured):
    settings, manifest, _ = configured
    runtime = bootstrap.assemble_business_runtime(settings, manifest, resource_check=lambda: None)
    assert runtime.business_workflow.expert_prompt == "合成专家模板"
    assert runtime.business_workflow.report_prompt == "合成报告模板"
    assert runtime.force_engineering_exports and runtime.development_outbound_enabled
    assert runtime.max_active_runs == 1
    app = FastAPI()
    install_business_runtime(app, runtime)
    assert get_report_run_service in app.dependency_overrides
    with pytest.raises(ValueError):
        install_business_runtime(app, replace(runtime, business_workflow=None))


@pytest.mark.parametrize("change,code", [
    ("digest", "money_guard_contract_configuration_changed"),
    ("model", "money_guard_contract_configuration_changed"),
    ("endpoint", "money_guard_contract_configuration_changed"),
    ("quote", "money_guard_contract_quote_invalid"),
    ("mode", "money_guard_billing_mode_invalid"),
])
def test_assembly_rejects_billing_contract_drift(configured, change, code):
    settings, manifest, billing = configured
    contract = billing["contracts"]["generator"]
    if change == "digest":
        billing["billing_contract_digest"] = "0" * 64
    else:
        updates = {
            "model": {"model": "another-model"},
            "endpoint": {"endpoint_digest": "0" * 64},
            "quote": {"quote_cny": Decimal(0)},
            "mode": {"billing_mode": "local_token_free"},
        }
        billing["contracts"]["generator"] = replace(contract, **updates[change])
    with pytest.raises(HarnessError, match=code):
        bootstrap.assemble_business_runtime(settings, manifest, resource_check=lambda: None)


def test_assembly_does_not_silently_drop_provider_routing(configured):
    settings, manifest, _ = configured
    settings.models.report_external.endpoints[0].extra_body = {"provider": {"order": ["other"]}}
    with pytest.raises(HarnessError, match="runtime_request_options_unapproved"):
        bootstrap.assemble_business_runtime(settings, manifest, resource_check=lambda: None)


def test_assembly_does_not_create_missing_ledger(configured, tmp_path):
    settings, manifest, _ = configured
    path = tmp_path / "missing.sqlite3"
    manifest.ledger_path = str(path)
    with pytest.raises(HarnessError, match="money_guard_ledger_missing"):
        bootstrap.assemble_business_runtime(settings, manifest, resource_check=lambda: None)
    assert not path.exists()


def test_connection_key_preserves_original_environment_name_resolution(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_REPORT_KEY", "synthetic-secret")
    profile = SimpleNamespace(
        api_key_env=None, connection=SimpleNamespace(key="SYNTHETIC_REPORT_KEY"),
    )
    assert bootstrap._headers(profile) == {"Authorization": "Bearer synthetic-secret"}
    monkeypatch.delenv("SYNTHETIC_REPORT_KEY")
    with pytest.raises(HarnessError, match="runtime_credentials_unavailable") as error:
        bootstrap._headers(profile)
    assert "SYNTHETIC_REPORT_KEY" not in str(error.value)


@pytest.mark.parametrize("attempts", [[True], ["3"], [0], [3, 3]])
def test_unknown_attempt_acknowledgement_cannot_be_coerced(configured, attempts):
    _, manifest, _ = configured
    data = manifest.model_dump()
    data["acknowledged_unknown_attempts"] = attempts
    with pytest.raises(ValueError, match="正整数"):
        bootstrap.BusinessRuntimeManifest.model_validate(data)
