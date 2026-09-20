from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

from app.report_harness.authorization import AuthorizationCatalog
from app.report_harness.business_workflow import BusinessWorkflow
from app.report_harness.release_registry import ReleaseRegistry
from app.schemas.report_run import BudgetPolicy


class ReportRoles(Protocol):
    """角色只返回结果；状态、快照、权限和发布始终由控制器管理。"""

    async def prepare(self, snapshot: dict) -> dict: ...

    async def generate(self, context: dict) -> dict: ...

    async def review(self, context: dict) -> dict: ...

    async def close(self) -> None: ...


@dataclass(frozen=True)
class ReportExecutionDependencies:
    roles_factory: Callable[[], ReportRoles]
    execution_profile: Literal["outbound", "synthetic_test"]
    endpoint_profile_digest: str
    policy_digest: str
    knowledge_manifest_digest: str
    max_active_seconds: float = 600
    authorization_catalog: AuthorizationCatalog | None = None
    knowledge_chunks: tuple[dict, ...] = ()
    budget_policy: BudgetPolicy = field(default_factory=BudgetPolicy)
    runtime_roles_factory: Callable[..., ReportRoles] | None = None
    release_registry: ReleaseRegistry | None = None
    force_engineering_exports: bool = False
    code_digest: str | None = None
    development_outbound_enabled: bool = False
    production_outbound_enabled: bool = False
    business_workflow: BusinessWorkflow | None = None
    external_knowledge_source: bool = False
    max_active_runs: int | None = None
    resource_check: Callable[[], None] | None = None

    def __post_init__(self):
        if self.max_active_runs is not None and (
            type(self.max_active_runs) is not int or self.max_active_runs < 1
        ):
            raise ValueError("活动运行容量必须为正整数。")
        if self.resource_check is not None and not callable(self.resource_check):
            raise TypeError("资源检查必须可调用。")
        if type(self.development_outbound_enabled) is not bool:
            raise TypeError("development_outbound_enabled 必须是严格布尔值。")
        if type(self.production_outbound_enabled) is not bool:
            raise TypeError("production_outbound_enabled 必须是严格布尔值。")
        if self.production_outbound_enabled:
            if (self.development_outbound_enabled or self.force_engineering_exports
                    or self.execution_profile != "outbound"
                    or self.runtime_roles_factory is None
                    or self.business_workflow is None
                    or self.authorization_catalog is None
                    or self.release_registry is None or not self.code_digest
                    or len(self.code_digest) != 64
                    or any(c not in "0123456789abcdef" for c in self.code_digest)
                    or self.resource_check is None):
                raise ValueError("正式运行必须具备完整业务、资源检查和有效发布绑定，不得混用开发模式。")
        if self.max_active_seconds <= 0:
            raise ValueError("运行时间预算必须为正。")
        if self.development_outbound_enabled:
            if self.execution_profile != "outbound":
                raise ValueError("开发 outbound 依赖必须使用 outbound 执行档位。")
            if self.force_engineering_exports is not True:
                raise ValueError("开发 outbound 依赖必须强制工程导出。")
            if self.authorization_catalog is None:
                raise ValueError("开发 outbound 依赖必须提供许可目录。")
            if self.runtime_roles_factory is None:
                raise ValueError("开发 outbound 依赖必须提供运行时角色工厂。")
            if self.release_registry is not None or self.code_digest is not None:
                raise ValueError("开发 outbound 依赖不得携带正式发布绑定。")


def production_dependencies(config: dict) -> None:
    """正式入口只接受服务端运行配置，不接受测试执行器或客户端批准表。"""
    if config.get("execution_profile") == "synthetic_test" or config.get("test_release_bindings"):
        raise ValueError("生产配置禁止测试执行器或测试批准表。")
    if config.get("online_enabled") and (
        not config.get("enabled") or not config.get("runtime_manifest_path")
        or not config.get("resource_paths")
    ):
        raise ValueError("正式运行缺少服务端配置或资源检查路径。")
    return None
