from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from app.core.exceptions import ConfigurationError
from app.core.settings import get_api_key
from app.providers.llm.lmstudio_compat import build_chat_completions_url
from app.report_harness.authorization import AuthorizationCatalog, EndpointDescription
from app.report_harness.business_workflow import BusinessWorkflow
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.knowledge_assets import load_knowledge_assets
from app.report_harness.money_guard import (
    RoleBillingContract,
    VersionedMoneyGuardHTTPAttemptClient,
    read_billing_contract,
)
from app.report_harness.runtime_factory import build_business_dependencies
from app.report_harness.runtime_profiles import (
    ModelCapacity,
    capacity_budget,
    capacity_from_expert_metadata,
    capacity_from_local_embedding_metadata,
    capacity_from_metadata,
    openrouter_price_filter,
    price_upper_cny,
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


ROLES = frozenset({"expert", "generator", "reviewer", "embedding"})


@dataclass(frozen=True)
class RoleBindings:
    """四个业务角色的端点、容量证明与报告端点配置；运行时装配与建账共用同一份计算。"""

    endpoints: dict[str, str]
    capacities: dict[str, ModelCapacity]
    profiles: dict
    local_embedding: bool


def _require_role_metadata(manifest: BusinessRuntimeManifest) -> None:
    if set(manifest.metadata) != ROLES:
        raise HarnessError("model_capacity_unverified")


def resolve_role_bindings(settings, manifest: BusinessRuntimeManifest, *,
                          local_embedding: bool) -> RoleBindings:
    _require_role_metadata(manifest)
    expert = settings.models.expert_local
    embedding = settings.models.retrieval_embedding
    report = settings.models.report_external
    if local_embedding and embedding.base_url.rstrip("/") != expert.base_url.rstrip("/"):
        raise HarnessError("local_embedding_endpoint_unapproved")
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
        "expert": build_chat_completions_url(expert.base_url),
        "embedding": embedding.base_url.rstrip("/") + "/embeddings",
        **{role: profile.url for role, profile in profiles.items()},
    }
    capacities = {
        "expert": capacity_from_expert_metadata(manifest.metadata["expert"], model=expert.model),
        "embedding": (
            capacity_from_local_embedding_metadata(manifest.metadata["embedding"], model=embedding.model)
            if local_embedding else capacity_from_metadata(
                manifest.metadata["embedding"], model=embedding.model, effort=None, embedding=True,
            )
        ),
    }
    for role, profile in profiles.items():
        effort = profile.reasoning.effort if profile.reasoning else profile.reasoning_effort
        if effort is None:
            raise HarnessError("model_effort_not_highest")
        capacities[role] = capacity_from_metadata(
            manifest.metadata[role], model=profile.model or report.model, effort=effort,
        )
    return RoleBindings(endpoints, capacities, profiles, local_embedding)


def expected_contracts(bindings: RoleBindings, metadata: dict[str, dict], *,
                       usd_to_cny: Decimal) -> dict[str, RoleBillingContract]:
    """按当前端点与容量证明应登记的合同；报价取模型完整容量下的保守费用上限。"""
    contracts = {}
    for role, capacity in bindings.capacities.items():
        free = role == "expert" or (role == "embedding" and bindings.local_embedding)
        contracts[role] = RoleBillingContract(
            role=role, model=capacity.model,
            endpoint_digest=billing_endpoint_digest(role, bindings.endpoints[role], capacity),
            quote_cny=Decimal(0) if free else price_upper_cny(
                metadata[role], capacity, usd_to_cny=usd_to_cny,
            ),
            billing_mode="local_token_free" if free else "remote_actual",
            usage_source="self_hosted_usage" if free else "cost_usd",
        )
    return contracts


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
    _require_role_metadata(manifest)
    ledger = Path(manifest.ledger_path)
    if not ledger.is_absolute() or not ledger.is_file():
        raise HarnessError("money_guard_ledger_missing")
    billing = read_billing_contract(ledger, manifest.experiment_id)
    if billing["billing_contract_digest"] != manifest.billing_contract_digest:
        raise HarnessError("money_guard_contract_configuration_changed")
    expert = settings.models.expert_local
    embedding = settings.models.retrieval_embedding
    report = settings.models.report_external
    contracts = billing["contracts"]
    bindings = resolve_role_bindings(settings, manifest, local_embedding=(
        "embedding" in contracts and contracts["embedding"].billing_mode == "local_token_free"
    ))
    endpoints, capacities, profiles = bindings.endpoints, bindings.capacities, bindings.profiles
    local_embedding = bindings.local_embedding
    if set(contracts) != set(capacities):
        raise HarnessError("money_guard_roles_unregistered")
    expected = expected_contracts(bindings, manifest.metadata, usd_to_cny=billing["usd_to_cny_upper"])
    for role, want in expected.items():
        contract = contracts[role]
        if contract.model != want.model or contract.endpoint_digest != want.endpoint_digest:
            raise HarnessError("money_guard_contract_configuration_changed")
        if contract.billing_mode != want.billing_mode:
            raise HarnessError("money_guard_billing_mode_invalid")
        if contract.quote_cny < want.quote_cny:
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
        if role == "embedding" and local_embedding:
            continue
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

    dependencies = build_business_dependencies(
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
    runtime_factory = dependencies.runtime_roles_factory
    if runtime_factory is None:
        raise HarnessError("runtime_factory_required")

    async def structured_runtime_factory(*args, **kwargs):
        roles = await runtime_factory(*args, **kwargs)
        roles.models["generator"] = replace(roles.models["generator"], json_object_mode=True)
        roles.models["reviewer"] = replace(roles.models["reviewer"], json_object_mode=True)
        return roles

    return replace(dependencies, runtime_roles_factory=structured_runtime_factory)
