from __future__ import annotations

from copy import deepcopy
from typing import Literal

from pydantic import AnyHttpUrl, Field, field_validator

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.schemas.base import StrictModel


class EndpointDescription(StrictModel):
    role: Literal["expert", "generator", "reviewer", "embedding"]
    label: str = Field(min_length=1, max_length=200)
    base_url: AnyHttpUrl
    model: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=200)

    @field_validator("base_url")
    @classmethod
    def no_url_credentials(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("端点说明不可携带凭据、查询参数或片段。")
        return value


class KnowledgeCollection(StrictModel):
    collection_id: str = Field(min_length=1, max_length=200)
    version: str = Field(min_length=1, max_length=200)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    label: str = Field(min_length=1, max_length=200)


class AuthorizationRequest(StrictModel):
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    endpoint_profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_knowledge_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: bool = Field(strict=True)


class AuthorizationCatalog:
    """只读的服务端许可目录；描述不包含密钥，也不执行端点探测。"""

    def __init__(self, endpoints: list[EndpointDescription],
                 knowledge: list[KnowledgeCollection], allowed_manifests: frozenset[str]):
        roles = [endpoint.role for endpoint in endpoints]
        if len(roles) != len(set(roles)) or not {"generator", "reviewer"}.issubset(roles):
            raise ValueError("端点角色必须唯一并包含生成与独立审查。")
        if len({entry.collection_id for entry in knowledge}) != len(knowledge):
            raise ValueError("知识集合不可重复。")
        self._endpoints = [item.model_dump(mode="json") for item in endpoints]
        self._knowledge = [item.model_dump(mode="json") for item in knowledge]
        self.endpoint_digest = canonical_digest(self._endpoints)
        self.knowledge_digest = canonical_digest(self._knowledge)
        self.allowed_manifests = frozenset(allowed_manifests)

    def preview(self, record: dict) -> dict:
        available = (
            record["endpoint_profile_digest"] == self.endpoint_digest
            and record["snapshot"]["knowledge_manifest_digest"] == self.knowledge_digest
            and self.knowledge_digest in self.allowed_manifests
        )
        return {
            "available": available,
            "reason": None if available else "authorization_profile_unavailable",
            "snapshot_digest": record["snapshot_digest"],
            "endpoint_profile_digest": record["endpoint_profile_digest"],
            "approved_knowledge_manifest_digest": record["snapshot"]["knowledge_manifest_digest"],
            "snapshot": deepcopy(record["snapshot"]),
            "endpoints": deepcopy(self._endpoints) if available else [],
            "knowledge_collections": deepcopy(self._knowledge) if available else [],
        }

    def validate(self, record: dict, request: AuthorizationRequest) -> dict:
        preview = self.preview(record)
        if not preview["available"]:
            raise HarnessError("authorization_profile_unavailable")
        if not request.confirmed:
            raise HarnessError("authorization_not_confirmed")
        for key in ("snapshot_digest", "endpoint_profile_digest", "approved_knowledge_manifest_digest"):
            if getattr(request, key) != preview[key]:
                raise HarnessError("authorization_digest_conflict")
        return {
            "snapshot_digest": request.snapshot_digest,
            "endpoint_profile_digest": request.endpoint_profile_digest,
            "approved_knowledge_manifest_digest": request.approved_knowledge_manifest_digest,
        }
