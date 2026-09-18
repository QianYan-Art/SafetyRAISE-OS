from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from app.core.exceptions import ConfigurationError
from app.core.settings import get_api_key
from app.report_harness.authorization import AuthorizationCatalog, EndpointDescription
from app.report_harness.business_workflow import BusinessWorkflow
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.knowledge_assets import load_knowledge_assets
from app.report_harness.money_guard import (
    VersionedMoneyGuardHTTPAttemptClient, read_billing_contract,
)
from app.report_harness.runtime_factory import build_business_dependencies
from app.report_harness.runtime_profiles import (
    capacity_from_expert_metadata, capacity_from_metadata, price_upper_cny,
    openrouter_price_filter, capacity_budget,
)
from app.schemas.base import StrictModel
from app.schemas.report_run import BudgetPolicy


class BusinessRuntimeManifest(StrictModel):
    """由操作者批准的配置，不接受请求正文或模型工具修改。"""

    knowledge_content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    billing_contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    ledger_path: str = Field(min_length=1)
    experiment_id: str = Field(min_length=1)
    generator_endpoint_name: str = Field(min_length=1)
    reviewer_endpoint_name: str = Field(min_length=1)
    metadata: dict[str, dict]
    acknowledged_unknown_attempts: list[int] = Field(default_factory=list)
    budget: BudgetPolicy | None = None

    @field_validator("acknowledged_unknown_attempts", mode="before")
    @classmethod
    def strict_attempt_ids(cls, value):
        if (not isinstance(value, list)
                or any(type(item) is not int or item < 1 for item in value)
                or len(value) != len(set(value))):
            raise ValueError("未知请求确认必须是无重复的正整数编号。")
        return value


def billing_endpoint_digest(role, endpoint, capacity):
    return canonical_digest({
        "role": role, "endpoint": endpoint, "model": capacity.model,
        "capacity_proof_digest": capacity.proof_digest,
    })


def _headers(profile, fallback_key_env=None):
    name = profile.api_key_env or fallback_key_env
    inline = getattr(profile, "api_key", None)
    connection = getattr(profile, "connection", None)
    credential = (connection.key if connection and connection.key else name)
    try:
        key = inline or (get_api_key(credential) if credential else None)
    except ConfigurationError:
        raise HarnessError("runtime_credentials_unavailable", 503) from None
    return {"Authorization": "Bearer " + key} if key else {}


def assemble_business_runtime(settings, manifest: BusinessRuntimeManifest, *, resource_check):
    """只读装配；不探测模型、不注册货币合同、不初始化新预算或修改生产开关。"""
    if set(manifest.metadata) != {"expert", "generator", "reviewer", "embedding"}:
        raise HarnessError("model_capacity_unverified")
    ledger = Path(manifest.ledger_path)
    if not ledger.is_absolute() or not ledger.is_file():
        raise HarnessError("money_guard_ledger_missing")
    billing = read_billing_contract(ledger, manifest.experiment_id)
    if billing["billing_contract_digest"] != manifest.billing_contract_digest:
        raise HarnessError("money_guard_contract_configuration_changed")
    expert = settings.models.expert_local
    embedding = settings.models.retrieval_embedding
    report = settings.models.report_external
    endpoints_by_name = {item.name: item for item in report.endpoints}
    profiles = {}
    for role, name in (
        ("generator", manifest.generator_endpoint_name),
        ("reviewer", manifest.reviewer_endpoint_name),
    ):
        if name not in endpoints_by_name:
            raise HarnessError("authorization_profile_unavailable")
        profiles[role] = endpoints_by_name[name]
        # 尚未纳入容量和费用证明的路由选项不能被悄悄忽略。
        if profiles[role].extra_body:
            raise HarnessError("runtime_request_options_unapproved")
    endpoints = {
        "expert": expert.base_url.rstrip("/") + "/chat/completions",
        "embedding": embedding.base_url.rstrip("/") + "/embeddings",
        **{role: profile.url for role, profile in profiles.items()},
    }
    capacities = {
        "expert": capacity_from_expert_metadata(manifest.metadata["expert"], model=expert.model),
        "embedding": capacity_from_metadata(
            manifest.metadata["embedding"], model=embedding.model, effort=None, embedding=True,
        ),
    }
    for role, profile in profiles.items():
        effort = profile.reasoning.effort if profile.reasoning else profile.reasoning_effort
        if effort is None:
            raise HarnessError("model_effort_not_highest")
        capacities[role] = capacity_from_metadata(
            manifest.metadata[role], model=profile.model or report.model, effort=effort,
        )
    contracts = billing["contracts"]
    if set(contracts) != set(capacities):
        raise HarnessError("money_guard_roles_unregistered")
    for role, capacity in capacities.items():
        contract = contracts[role]
        if (contract.model != capacity.model or contract.endpoint_digest
                != billing_endpoint_digest(role, endpoints[role], capacity)):
            raise HarnessError("money_guard_contract_configuration_changed")
        expected_mode = "local_token_free" if role == "expert" else "remote_actual"
        if contract.billing_mode != expected_mode:
            raise HarnessError("money_guard_billing_mode_invalid")
        required = Decimal(0) if role == "expert" else price_upper_cny(
            manifest.metadata[role], capacity, usd_to_cny=billing["usd_to_cny_upper"],
        )
        if contract.quote_cny < required:
            raise HarnessError("money_guard_contract_quote_invalid")
    assets = load_knowledge_assets(
        settings, approved_content_digest=manifest.knowledge_content_digest,
    )
    catalog = AuthorizationCatalog(
        [EndpointDescription(
            role=role, label=role, base_url=endpoints[role], model=capacity.model,
            version=capacity.proof_digest,
        ) for role, capacity in capacities.items()],
        [assets.collection],
        frozenset({canonical_digest([assets.collection.model_dump(mode="json")])}),
    )
    headers = {
        "expert": _headers(expert), "embedding": _headers(embedding),
        **{role: _headers(profile, report.api_key_env) for role, profile in profiles.items()},
    }
    request_options = {}
    for role, profile in {"expert": expert, **profiles}.items():
        options = {}
        temperature = profile.temperature
        if temperature is None and role != "expert":
            temperature = report.temperature
        if temperature is not None:
            options["temperature"] = temperature
        verbosity = getattr(profile, "verbosity", None)
        if verbosity is not None:
            options["verbosity"] = verbosity
        request_options[role] = options
    for role in ("generator", "reviewer", "embedding"):
        address = urlsplit(endpoints[role])
        if address.scheme != "https" or address.hostname != "openrouter.ai":
            raise HarnessError("runtime_pricing_enforcement_unavailable")
        request_options.setdefault(role, {})["provider"] = openrouter_price_filter(
            manifest.metadata[role], capacities[role],
        )
    workflow = BusinessWorkflow(
        settings.guidance_prompt_file.read_text(encoding="utf-8"),
        settings.report_prompt_file.read_text(encoding="utf-8"),
    )
    frozen = manifest.model_copy(deep=True)
    budget = capacity_budget(capacities, frozen.budget)
    role_timeouts = {
        "expert": expert.timeout_seconds, "embedding": embedding.timeout_seconds,
        **{role: profile.timeout_seconds for role, profile in profiles.items()},
    }

    def monetary_client(client):
        return VersionedMoneyGuardHTTPAttemptClient(
            client, ledger_path=ledger, experiment_id=frozen.experiment_id,
            contracts=deepcopy(contracts), usd_to_cny_upper=billing["usd_to_cny_upper"],
            acknowledged_unknown_attempts=frozen.acknowledged_unknown_attempts,
        )

    return build_business_dependencies(
        workflow=workflow, capacities=capacities, endpoints=endpoints, headers=headers,
        catalog=catalog, knowledge_chunks=assets.chunks, budget_policy=budget,
        monetary_client_factory=monetary_client, retriever_factory=assets.retriever,
        validate_source=assets.validate, embedding_dimensions=embedding.dimensions,
        billing_contract_digest=frozen.billing_contract_digest, resource_check=resource_check,
        embedding_query_instruction=embedding.query_instruction,
        embedding_query_max_length=embedding.query_max_length, max_active_runs=1,
        request_options=request_options,
        role_timeouts=role_timeouts,
    )
