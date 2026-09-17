from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

from app.report_harness.authorization import AuthorizationCatalog
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

    def __post_init__(self):
        if self.max_active_seconds <= 0:
            raise ValueError("运行时间预算必须为正。")


def production_dependencies(config: dict) -> None:
    """数据接口可显式启用；在线 transport 未验收前保持关闭。"""
    if config.get("execution_profile") == "synthetic_test" or config.get("test_release_bindings"):
        raise ValueError("生产配置禁止测试执行器或测试批准表。")
    if config.get("online_enabled"):
        raise ValueError("报告运行尚未完成在线能力与质量验收。")
    return None
