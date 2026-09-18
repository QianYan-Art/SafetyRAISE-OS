from dataclasses import replace

import pytest

from app.report_harness.authorization import AuthorizationCatalog, EndpointDescription
from app.report_harness.business_workflow import BusinessWorkflow
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.runtime_factory import build_business_dependencies
from app.report_harness.runtime_profiles import ModelCapacity
from app.schemas.report_run import BudgetPolicy


def inputs():
    roles = ("expert", "generator", "reviewer", "embedding")
    capacities = {role: ModelCapacity(
        "synthetic-" + role, 1000, 0 if role == "embedding" else 100,
        canonical_digest(role), None if role in {"expert", "embedding"} else "high",
    ) for role in roles}
    endpoints = {role: "https://example.invalid/" + role for role in roles}
    catalog = AuthorizationCatalog([
        EndpointDescription(role=role, label=role, base_url=endpoints[role],
                            model=capacities[role].model, version=capacities[role].proof_digest)
        for role in roles
    ], [], frozenset({canonical_digest([])}))
    return {
        "workflow": BusinessWorkflow("合成专家模板", "合成报告模板"),
        "capacities": capacities, "endpoints": endpoints, "headers": {},
        "catalog": catalog, "knowledge_chunks": (),
        "budget_policy": BudgetPolicy(),
        "monetary_client_factory": lambda client: client,
        "retriever_factory": lambda embedding: None,
        "validate_source": lambda: None, "embedding_dimensions": 2,
        "billing_contract_digest": canonical_digest("合成货币合同"),
        "resource_check": lambda: None,
    }


def test_complete_factory_requires_all_roles_and_durable_money_adapter():
    kwargs = inputs()
    del kwargs["capacities"]["expert"]
    with pytest.raises(ValueError):
        build_business_dependencies(**kwargs)
    kwargs = inputs()
    kwargs["monetary_client_factory"] = None
    with pytest.raises(ValueError, match="货币预算"):
        build_business_dependencies(**kwargs)


def test_runtime_endpoint_cannot_differ_from_authorization_preview():
    kwargs = inputs()
    kwargs["endpoints"]["generator"] = "https://another.invalid/chat"
    with pytest.raises(HarnessError, match="authorization_profile_unavailable"):
        build_business_dependencies(**kwargs)


def test_factory_keeps_real_development_separate_from_formal_release():
    dependencies = build_business_dependencies(**inputs())
    assert dependencies.development_outbound_enabled is True
    assert dependencies.force_engineering_exports is True
    assert dependencies.release_registry is None
    assert dependencies.code_digest is None
    assert dependencies.business_workflow is not None
    assert dependencies.external_knowledge_source is True
    assert dependencies.runtime_roles_factory is not None
    with pytest.raises(HarnessError, match="runtime_factory_required"):
        dependencies.roles_factory()


def test_capacity_and_expert_effort_cannot_be_silently_overridden():
    kwargs = inputs()
    kwargs["capacities"]["expert"] = replace(kwargs["capacities"]["expert"], effort="none")
    with pytest.raises(ValueError, match="推理"):
        build_business_dependencies(**kwargs)
    kwargs = inputs()
    kwargs["budget_policy"] = kwargs["budget_policy"].model_copy(
        update={"max_output_tokens_per_request": 1},
    )
    with pytest.raises(ValueError, match="容量"):
        build_business_dependencies(**kwargs)
