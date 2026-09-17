from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from jsonpointer import JsonPointerException, resolve_pointer
from pydantic import Field, model_validator

from app.report_harness.contracts import canonical_digest
from app.schemas.base import StrictModel
from app.schemas.report_run import EvidenceRecord


class EvidenceWriteRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    records: list[EvidenceRecord] = Field(max_length=50)

    @model_validator(mode="after")
    def validate_records(self) -> "EvidenceWriteRequest":
        ids = [record.evidence_id for record in self.records]
        if len(ids) != len(set(ids)):
            raise ValueError("证据 ID 不得重复。")
        if sum(len(record.text) for record in self.records) > 64000:
            raise ValueError("证据文字总量不得超过 64000 字符。")
        allowed = set(ids)
        for record in self.records:
            if not set(record.conflicts_with).issubset(allowed):
                raise ValueError("冲突引用必须属于本次完整保存的会话证据。")
        return self


class EvidenceBindingError(ValueError):
    def __init__(self, warnings: list[dict]):
        super().__init__("补充证据的事故字段引用无效。")
        self.warnings = warnings


def audit_records(records: list[EvidenceRecord], owner: str) -> list[dict]:
    timestamp = datetime.now(timezone.utc).isoformat()
    return [{
        **record.model_dump(mode="json"), "recorded_by": owner, "updated_at": timestamp,
    } for record in records]


def field_warnings(records: list[dict], accident_data: Any) -> list[dict]:
    warnings = []
    for record in records:
        for conflict in record.get("field_conflicts", []):
            pointer = conflict["accident_field"]
            try:
                if accident_data is None:
                    raise JsonPointerException("缺少草稿")
                resolve_pointer(accident_data, pointer)
            except (JsonPointerException, TypeError, IndexError, KeyError):
                warnings.append({
                    "evidence_id": record["evidence_id"], "accident_field": pointer,
                    "code": "unresolved_field",
                    "suggestion": "请在证据编辑中选择最终事故输入内存在的字段。",
                })
    return warnings


def fact_obligations(value: Any, path: str = "") -> list[dict]:
    if isinstance(value, dict) and value:
        return [
            item for key, child in value.items()
            for item in fact_obligations(
                child, path + "/" + str(key).replace("~", "~0").replace("/", "~1"),
            )
        ]
    if isinstance(value, list) and value:
        return [
            item for index, child in enumerate(value)
            for item in fact_obligations(child, path + "/" + str(index))
        ]
    return [{
        "obligation_id": "fact:" + path,
        "source_refs": ["accident:" + path],
        "required_treatment": "正文覆盖、说明不确定或给出不相关理由",
    }]


def freeze_snapshot(accident_data: dict, records: list[dict], revision: int,
                    knowledge_manifest_digest: str) -> dict:
    warnings = field_warnings(records, accident_data)
    if warnings:
        raise EvidenceBindingError(warnings)
    obligations = fact_obligations(accident_data)
    for record in records:
        obligations.append({
            "obligation_id": "evidence:" + record["evidence_id"],
            "source_refs": ["evidence:" + record["evidence_id"]],
            "required_treatment": "保留来源和核实状态，说明冲突；不可把陈述或待核实内容提升为事实",
        })
    return {
        "canonicalization_version": 1,
        "accident_data": deepcopy(accident_data),
        "supplemental_records": deepcopy(records),
        "revision": revision,
        "knowledge_manifest_digest": knowledge_manifest_digest,
        "fact_obligations": obligations,
        "source_digests": {
            "accident": canonical_digest(accident_data),
            **{"evidence:" + record["evidence_id"]: canonical_digest(record) for record in records},
        },
    }
