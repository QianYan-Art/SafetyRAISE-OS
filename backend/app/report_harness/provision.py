"""为主应用的业务运行时登记费用合同并生成运行清单；全程不发送模型请求。

    python -m app.report_harness.provision \\
        --metadata model-metadata.json \\
        --ledger /var/lib/safetyraise/ledger/money.sqlite3 \\
        --experiment report-evidence-demo-20260923 \\
        --output /tmp/runtime-manifest.json [--create-experiment] [--confirm]

--metadata 是操作者核验过的四角色模型元数据（容量、单价、推理等级）。未加 --confirm 时只打印
将要登记的合同和知识库摘要，不写账本和清单。新建实验的额度固定为 money_guard.BUDGET_CNY；
已有实验只追加有变化的角色合同，旧费用与未知请求记录不改。知识库或模型配置变更后重新执行，
即可得到绑定新知识摘要的清单。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from decimal import Decimal
from pathlib import Path

from app.core.settings import load_settings
from app.report_harness.business_bootstrap import (
    BusinessRuntimeManifest, assemble_business_runtime, expected_contracts, resolve_role_bindings,
)
from app.report_harness.knowledge_assets import knowledge_asset_paths, knowledge_content_digest
from app.report_harness.money_guard import (
    ALLOWED_ROLES, MoneyGuardConfigurationError, MoneyGuardHTTPAttemptClient, RoleBillingContract,
    VersionedMoneyGuardHTTPAttemptClient, read_billing_contract,
)

# 新实验的记账参数：汇率取保守上限，单次请求预留上限不超过总额度。
USD_TO_CNY_UPPER = Decimal("10")
REQUEST_COST_UPPER_CNY = Decimal("24")
_PLACEHOLDER_DIGEST = "0" * 64


class _ContractMaintenance:
    """只用于建实验与登记合同的客户端；任何模型调用都会被拒绝。"""

    def __init__(self, roles):
        self.registered_roles = tuple(roles)

    async def attempt(self, *_args, **_kwargs):
        raise RuntimeError("建账过程禁止调用模型。")

    async def close(self):
        return None


def _existing_contract_state(ledger: Path, experiment_id: str) -> dict | None:
    """实验不存在时返回 None；账本损坏或不可读照常报错，不能被当成“尚未建账”。"""
    try:
        return read_billing_contract(ledger, experiment_id)
    except MoneyGuardConfigurationError as exc:
        if exc.code == "money_guard_ledger_missing":
            return None
        raise


def create_experiment(ledger: Path, experiment_id: str) -> None:
    client = MoneyGuardHTTPAttemptClient(
        _ContractMaintenance(ALLOWED_ROLES), ledger, experiment_id,
        cost_upper_cny=REQUEST_COST_UPPER_CNY, usd_to_cny_upper=USD_TO_CNY_UPPER,
    )
    asyncio.run(client.close())


def register_contracts(ledger: Path, experiment_id: str,
                       contracts: dict[str, RoleBillingContract]) -> str:
    """逐角色追加有变化的合同，返回最终的总合同摘要。"""
    client = VersionedMoneyGuardHTTPAttemptClient(
        _ContractMaintenance(sorted(contracts)), ledger, experiment_id,
    )
    try:
        for role, contract in sorted(contracts.items()):
            current = read_billing_contract(ledger, experiment_id)
            previous = current["contracts"].get(role)
            if previous == contract:
                continue
            client.register_contract(
                contract, current["billing_contract_digest"],
                expected_previous_role_digest=previous.digest if previous else None,
            )
    finally:
        asyncio.run(client.close())
    return read_billing_contract(ledger, experiment_id)["billing_contract_digest"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="登记 harness 费用合同并生成运行清单。")
    parser.add_argument("--config", default=None, help="工作流配置路径，缺省同主应用。")
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--ledger", required=True, type=Path)
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--generator-endpoint", default="openrouter_primary")
    parser.add_argument("--reviewer-endpoint", default="openrouter_primary")
    parser.add_argument("--max-active-seconds", type=float, default=7200,
                        help="单次报告的活动时长上限；专家端点冷启动较慢，默认 7200 秒。")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--create-experiment", action="store_true", help="新建实验（额度固定）。")
    parser.add_argument("--confirm", action="store_true", help="实际写入账本和清单。")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    metadata = json.loads(args.metadata.read_text(encoding="utf-8"))
    ledger = args.ledger.absolute()
    existing = _existing_contract_state(ledger, args.experiment)
    if args.create_experiment == (existing is not None):
        parser.error("实验已存在时不能 --create-experiment；实验不存在时必须显式新建。")
    draft = BusinessRuntimeManifest(
        knowledge_content_digest=knowledge_content_digest(knowledge_asset_paths(settings)),
        billing_contract_digest=_PLACEHOLDER_DIGEST, ledger_path=str(ledger),
        experiment_id=args.experiment, generator_endpoint_name=args.generator_endpoint,
        reviewer_endpoint_name=args.reviewer_endpoint, metadata=metadata,
    )
    bindings = resolve_role_bindings(settings, draft, local_embedding=False)
    usd_to_cny = existing["usd_to_cny_upper"] if existing else USD_TO_CNY_UPPER
    contracts = expected_contracts(bindings, metadata, usd_to_cny=usd_to_cny)
    print(json.dumps({
        "experiment": args.experiment, "ledger": str(ledger), "create_experiment": args.create_experiment,
        "knowledge_content_digest": draft.knowledge_content_digest,
        "contracts": {role: contract.to_dict() for role, contract in sorted(contracts.items())},
    }, ensure_ascii=False, indent=2))
    if not args.confirm:
        print("未加 --confirm：未写入账本和清单。")
        return 0
    if args.output.exists():
        parser.error(f"{args.output} 已存在，拒绝覆盖。")

    if args.create_experiment:
        create_experiment(ledger, args.experiment)
    digest = register_contracts(ledger, args.experiment, contracts)
    manifest = draft.model_copy(update={"billing_contract_digest": digest})
    # 用真实装配完整校验一遍（合同、容量、知识库、凭据），通过后才写出清单。
    runtime = assemble_business_runtime(settings, manifest, resource_check=lambda: None)
    budget = runtime.budget_policy.model_copy(update={"max_active_seconds": args.max_active_seconds})
    manifest = manifest.model_copy(update={"budget": budget})
    args.output.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    print(f"已写入运行清单：{args.output}（总合同摘要 {digest[:16]}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
