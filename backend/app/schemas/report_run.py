from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.base import StrictModel


EvidenceKind = Literal["observation", "statement", "document_excerpt", "other"]
VerificationStatus = Literal["unverified", "human_confirmed", "disputed"]
ClaimType = Literal["fact", "inference", "knowledge"]
ObligationTreatment = Literal["covered", "uncertain", "not_relevant"]
CheckCategory = Literal["facts", "coverage", "reasoning", "citations", "conciseness"]
IssueSeverity = Literal["blocker", "major", "minor"]
IssueStatus = Literal["open", "resolved", "contested"]


def _strip_text(value: object) -> object:
    if isinstance(value, str):
        return value.strip()
    return value


def _validate_payload_size(model: StrictModel) -> None:
    try:
        encoded = json.dumps(model.model_dump(mode="json"), ensure_ascii=False,
                             allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("内容必须可编码为有效 JSON。") from exc
    if len(encoded) > 256 * 1024:
        raise ValueError("结构化内容不得超过 256 KiB。")


class TextSpan(StrictModel):
    start: int = Field(ge=0)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_order(self) -> "TextSpan":
        if self.end <= self.start:
            raise ValueError("正文定位的 end 必须大于 start。")
        return self


class FieldConflict(StrictModel):
    accident_field: str
    explanation: str = Field(min_length=1)

    @field_validator("accident_field", "explanation", mode="before")
    @classmethod
    def _strip_fields(cls, value: object) -> object:
        return _strip_text(value)

    @field_validator("accident_field")
    @classmethod
    def _validate_json_pointer(cls, value: str) -> str:
        if value == "":
            return value
        if not value.startswith("/"):
            raise ValueError("accident_field 必须是 JSON Pointer。")
        for token in value.split("/")[1:]:
            if re.search(r"~(?![01])", token):
                raise ValueError("accident_field 的 JSON Pointer 转义必须使用 ~0 或 ~1。")
        return value


class EvidenceRecord(StrictModel):
    evidence_id: UUID
    text: str = Field(min_length=1, max_length=8000)
    source_label: str = Field(min_length=1, max_length=200)
    source_locator: str = Field(min_length=1, max_length=500)
    kind: EvidenceKind
    verification_status: VerificationStatus
    conflicts_with: list[UUID] = Field(default_factory=list)
    verification_note: str | None = Field(default=None, max_length=1000)
    field_conflicts: list[FieldConflict] = Field(default_factory=list)

    @field_validator(
        "text",
        "source_label",
        "source_locator",
        "verification_note",
        mode="before",
    )
    @classmethod
    def _strip_text_fields(cls, value: object) -> object:
        return _strip_text(value)

    @field_validator("source_locator")
    @classmethod
    def _reject_external_locator(cls, value: str) -> str:
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
            raise ValueError("source_locator 不能是 URL。")
        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(("/", "\\\\")):
            raise ValueError("source_locator 不能是可执行路径。")
        return value

    @model_validator(mode="after")
    def _validate_human_fields(self) -> "EvidenceRecord":
        if self.verification_status in {"human_confirmed", "disputed"}:
            if not self.verification_note:
                raise ValueError("human_confirmed 或 disputed 必须提供 verification_note。")
        if self.evidence_id in self.conflicts_with:
            raise ValueError("conflicts_with 不能引用自身。")
        if len(set(self.conflicts_with)) != len(self.conflicts_with):
            raise ValueError("conflicts_with 不能包含重复 evidence_id。")
        return self


class CreateRunRequest(StrictModel):
    request_id: UUID
    session_id: str = Field(min_length=1)
    accident_data: dict[str, Any]
    evidence_revision: int = Field(ge=0)
    parent_run_id: UUID | None = None

    @field_validator("session_id", mode="before")
    @classmethod
    def _strip_session_id(cls, value: object) -> object:
        return _strip_text(value)

    @model_validator(mode="after")
    def _validate_accident_data(self) -> "CreateRunRequest":
        # 与旧 GenerateReportRequest 保持相同的空事故字典约束。
        if not self.accident_data:
            raise ValueError("accident_data 不能为空对象。")
        try:
            json.dumps(self.accident_data, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("accident_data 必须只包含有效 JSON 值。") from exc
        _validate_payload_size(self)
        return self


class ExecuteRunRequest(StrictModel):
    expected_version: int = Field(ge=0)


class BudgetPolicy(StrictModel):
    max_physical_requests: int = Field(default=24, ge=0)
    max_tool_calls: int = Field(default=24, ge=0)
    max_revision_rounds: int = Field(default=2, ge=0)
    max_retrieval_requests: int = Field(default=8, ge=0)
    max_active_seconds: int = Field(default=600, ge=0)
    max_total_tokens: int = Field(default=120000, ge=0)
    max_output_tokens_per_request: int = Field(default=8192, ge=0)
    max_money: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_request_budgets(self) -> "BudgetPolicy":
        if self.max_retrieval_requests > self.max_physical_requests:
            raise ValueError("max_retrieval_requests 不能超过 max_physical_requests。")
        return self


class Claim(StrictModel):
    claim_id: str = Field(min_length=1)
    text_span: TextSpan
    type: ClaimType
    evidence_refs: list[str] = Field(default_factory=list)
    knowledge_refs: list[str] = Field(default_factory=list)

    @field_validator("claim_id", mode="before")
    @classmethod
    def _strip_claim_id(cls, value: object) -> object:
        return _strip_text(value)

    @field_validator("evidence_refs", "knowledge_refs")
    @classmethod
    def _validate_refs(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("引用 ID 不能是空字符串。")
        if len(set(normalized)) != len(normalized):
            raise ValueError("同一断言不能重复引用同一 ID。")
        return normalized


class ObligationResolution(StrictModel):
    obligation_id: str = Field(min_length=1)
    treatment: ObligationTreatment
    resolution: str = Field(min_length=1)

    @field_validator("obligation_id", "resolution", mode="before")
    @classmethod
    def _strip_fields(cls, value: object) -> object:
        return _strip_text(value)


class CoverageCheck(StrictModel):
    obligation_id: str = Field(min_length=1)
    passed: bool
    claim_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    knowledge_refs: list[str] = Field(default_factory=list)
    conclusion: str = Field(min_length=1)

    @field_validator("obligation_id", "conclusion", mode="before")
    @classmethod
    def _strip_fields(cls, value: object) -> object:
        return _strip_text(value)

    @field_validator("claim_ids", "evidence_refs", "knowledge_refs")
    @classmethod
    def _validate_ref_lists(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("检查引用 ID 不能是空字符串。")
        if len(set(normalized)) != len(normalized):
            raise ValueError("检查引用 ID 不能重复。")
        return normalized


class CompletedCheck(StrictModel):
    category: CheckCategory
    passed: bool
    evidence_refs: list[str] = Field(default_factory=list)
    knowledge_refs: list[str] = Field(default_factory=list)
    conclusion: str = Field(min_length=1)

    @field_validator("conclusion", mode="before")
    @classmethod
    def _strip_conclusion(cls, value: object) -> object:
        return _strip_text(value)

    @field_validator("evidence_refs", "knowledge_refs")
    @classmethod
    def _validate_refs(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("检查引用 ID 不能是空字符串。")
        if len(set(normalized)) != len(normalized):
            raise ValueError("检查引用 ID 不能重复。")
        return normalized


class Issue(StrictModel):
    issue_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    severity: IssueSeverity
    target: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    source_refs: list[str] = Field(default_factory=list)
    closure_condition: str = Field(min_length=1)
    status: IssueStatus

    @field_validator(
        "issue_id",
        "category",
        "target",
        "explanation",
        "closure_condition",
        mode="before",
    )
    @classmethod
    def _strip_text_fields(cls, value: object) -> object:
        return _strip_text(value)

    @field_validator("source_refs")
    @classmethod
    def _validate_source_refs(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("问题来源引用不能是空字符串。")
        if len(set(normalized)) != len(normalized):
            raise ValueError("问题来源引用不能重复。")
        return normalized


class CandidateReport(StrictModel):
    version: int = Field(ge=0)
    report_markdown: str = Field(min_length=1)
    claims: list[Claim] = Field(default_factory=list)
    obligation_resolutions: list[ObligationResolution] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_size(self) -> "CandidateReport":
        _validate_payload_size(self)
        return self

    @field_validator("report_markdown", mode="before")
    @classmethod
    def _validate_report_text(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("report_markdown 不能为空。")
        return value


class ReviewResult(StrictModel):
    candidate_digest: str = Field(min_length=1)
    snapshot_digest: str = Field(min_length=1)
    coverage_checks: list[CoverageCheck] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    completed_checks: list[CompletedCheck] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_size(self) -> "ReviewResult":
        _validate_payload_size(self)
        return self
