from typing import Literal

from pydantic import Field

from app.report_harness.authorization import EndpointDescription, KnowledgeCollection
from app.schemas.base import StrictModel
from app.schemas.report_run import BudgetPolicy


class ReportHarnessSettings(StrictModel):
    enabled: bool = False
    online_enabled: bool = False
    runtime_manifest_path: str | None = None
    resource_paths: list[str] = Field(default_factory=list)
    minimum_free_mib: int = Field(default=2048, ge=256)
    minimum_memory_mib: int = Field(default=256, ge=64)
    execution_profile: Literal["outbound"] = "outbound"
    policy_version: str = "report-evidence-v1"
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    endpoints: list[EndpointDescription] = Field(default_factory=list)
    knowledge_collections: list[KnowledgeCollection] = Field(default_factory=list)
    approved_knowledge_manifests: list[str] = Field(default_factory=list)
