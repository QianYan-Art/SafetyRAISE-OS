from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict

from app.report_harness.authorization import AuthorizationCatalog
from app.report_harness.business_roles import BusinessTransportRoles
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.execution import ReportExecutionDependencies
from app.report_harness.knowledge_bridge import KnowledgeBridge, MeteredEmbedding
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.runtime_profiles import ConfiguredAttemptClient
from app.report_harness.transport import BudgetedTransport
from app.report_harness.transport_roles import RoleModel


class BusinessRuntimeRoles(BusinessTransportRoles):
    def __init__(self, transport, models, *, business_prompts, embedding, knowledge):
        super().__init__(transport, models, business_prompts=business_prompts)
        self.embedding = embedding
        self.knowledge = knowledge

    def search_knowledge(self, query, top_k):
        return self.knowledge.search(query, top_k)

    def retrieve_initial_knowledge(self, query, top_k):
        return self.knowledge.initial(query, top_k)

    async def close(self):
        self.embedding.close()
        await super().close()


def build_business_dependencies(*, workflow, capacities, endpoints, headers,
                                catalog: AuthorizationCatalog, knowledge_chunks,
                                budget_policy, monetary_client_factory, retriever_factory,
                                validate_source, embedding_dimensions,
                                billing_contract_digest, resource_check,
                                embedding_query_instruction="", embedding_query_max_length=1000,
                                max_active_runs=1, request_options=None, role_timeouts=None):
    """完整业务开发路径；货币账本必须显式注入，不借接入过程取得正式发布资格。"""
    required_roles = {"expert", "generator", "reviewer", "embedding"}
    if set(capacities) != required_roles or set(endpoints) != required_roles:
        raise ValueError("完整业务运行必须登记专家、生成、独立审查和嵌入四个端点。")
    catalog.validate_runtime(endpoints, capacities)
    if not callable(monetary_client_factory):
        raise ValueError("完整业务运行必须提供持久货币预算客户端。")
    if (not isinstance(billing_contract_digest, str) or len(billing_contract_digest) != 64
            or any(char not in "0123456789abcdef" for char in billing_contract_digest)):
        raise ValueError("必须绑定已经登记的货币合同摘要。")
    if not callable(retriever_factory) or not callable(validate_source):
        raise ValueError("必须提供受控检索工厂和知识版本验证。")
    if not callable(resource_check):
        raise ValueError("完整业务运行必须提供存储与内存资源检查。")
    if capacities["expert"].effort is not None:
        raise ValueError("专家模型保留原推理模式，不发送额外推理开关。")
    if budget_policy.max_output_tokens_per_request < max(
        capacity.output_tokens for capacity in capacities.values()
    ):
        raise ValueError("内部费用预留容量不足，不得改成模型输出限制。")
    if budget_policy.max_money is not None:
        raise ValueError("本路径货币预算由注入的持久账本管理，不同时启用未报价的token账本货币字段。")
    source = tuple(deepcopy(knowledge_chunks))
    capacity_profiles = dict(capacities)
    addresses, credentials = deepcopy(endpoints), deepcopy(headers)
    options = deepcopy(request_options or {})
    timeouts = dict(role_timeouts or {})
    policy_digest = canonical_digest({
        "business_prompts": workflow.prompts(),
        "retrieval_policy": workflow.retrieval_policy(),
        "capacity_profiles": {role: asdict(item) for role, item in capacities.items()},
        "budget": budget_policy.model_dump(mode="json"),
        "embedding_dimensions": embedding_dimensions,
        "query_instruction": embedding_query_instruction,
        "query_max_length": embedding_query_max_length,
        "max_active_runs": max_active_runs,
        "billing_contract_digest": billing_contract_digest,
        "credential_binding": canonical_digest(credentials),
        "request_options": options,
        "role_timeouts": timeouts,
    })

    async def factory(store, owner, run_id, token):
        record = store.get(owner, run_id)
        validate_source()
        if record["business_prompts"] != workflow.prompts():
            raise HarnessError("authorization_stale")
        elapsed_before = record.get("active_seconds", 0)
        started = asyncio.get_running_loop().time()

        def authorize():
            store.assert_active(owner, run_id, token)
            validate_source()
            if resource_check is not None:
                resource_check()
            current = store.get(owner, run_id)
            approval = current.get("approval")
            if not approval or any((
                approval.get("owner_user_id") != owner,
                approval.get("snapshot_digest") != current["snapshot_digest"],
                approval.get("endpoint_profile_digest") != catalog.endpoint_digest,
                approval.get("approved_knowledge_manifest_digest") != catalog.knowledge_digest,
                approval.get("policy_digest") != policy_digest,
            )):
                raise HarnessError("authorization_required")

        raw_client = ConfiguredAttemptClient(
            addresses, credentials, capacity_profiles, request_options=options, role_timeouts=timeouts,
        )
        client = raw_client
        embedding = None
        try:
            client = monetary_client_factory(raw_client)
            if (client is raw_client or getattr(client, "billing_contract_digest", None)
                    != billing_contract_digest):
                raise ValueError("实际货币客户端与冻结合同不一致。")
            if set(client.registered_roles) != required_roles:
                raise ValueError("货币客户端没有覆盖全部业务角色。")
            transport = BudgetedTransport(
                RequestLedger(store), client, owner=owner, run_id=run_id, token=token,
                endpoint_digest=catalog.endpoint_digest,
                output_limit=max(cap.output_tokens for cap in capacity_profiles.values()),
                review_reserve_tokens=capacity_profiles["reviewer"].bound().total_tokens,
                generation_reserve_tokens=capacity_profiles["generator"].bound().total_tokens,
                authorize=authorize,
                remaining_seconds=lambda: (
                    budget_policy.max_active_seconds - elapsed_before
                    - (asyncio.get_running_loop().time() - started)
                ),
                bound_provider=lambda role, payload: capacity_profiles[role].bound(),
                verified_proofs=frozenset(item.proof_digest for item in capacity_profiles.values()),
            )
            embedding = MeteredEmbedding(
                transport, model=capacity_profiles["embedding"].model,
                dimensions=embedding_dimensions, query_instruction=embedding_query_instruction,
                query_max_length=embedding_query_max_length,
            )
            retriever = retriever_factory(embedding)
            if retriever.reranker_client is not None:
                raise ValueError("未登记独立计费与权限的重排客户端不可进入完整业务路径。")
            bridge = KnowledgeBridge(retriever, source, validate_source=validate_source)
            return BusinessRuntimeRoles(
                transport, {role: RoleModel(capacity_profiles[role].model)
                            for role in ("expert", "generator", "reviewer")},
                business_prompts=record["business_prompts"], embedding=embedding, knowledge=bridge,
            )
        except BaseException:
            if embedding is not None:
                embedding.close()
            await client.close()
            if client is not raw_client:
                await raw_client.close()
            raise

    def unavailable():
        raise HarnessError("runtime_factory_required")

    return ReportExecutionDependencies(
        roles_factory=unavailable, runtime_roles_factory=factory,
        execution_profile="outbound", endpoint_profile_digest=catalog.endpoint_digest,
        policy_digest=policy_digest, knowledge_manifest_digest=catalog.knowledge_digest,
        authorization_catalog=catalog, knowledge_chunks=source,
        budget_policy=budget_policy, max_active_seconds=budget_policy.max_active_seconds,
        development_outbound_enabled=True, force_engineering_exports=True,
        business_workflow=workflow, external_knowledge_source=True,
        max_active_runs=max_active_runs, resource_check=resource_check,
    )
