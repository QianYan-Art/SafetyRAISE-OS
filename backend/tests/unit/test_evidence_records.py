from uuid import uuid4

import pytest

from app.report_harness.evidence import (
    EvidenceBindingError, EvidenceWriteRequest, audit_records, field_warnings, freeze_snapshot,
)


def evidence(**changes):
    return {
        "evidence_id": str(uuid4()), "text": "合成当事人陈述，尚待核实。",
        "source_label": "合成记录", "source_locator": "第一段",
        "kind": "statement", "verification_status": "unverified", **changes,
    }


def test_evidence_reference_scope_and_duplicate_ids():
    record = evidence(conflicts_with=[str(uuid4())])
    with pytest.raises(ValueError):
        EvidenceWriteRequest(expected_revision=0, records=[record])
    record = evidence()
    with pytest.raises(ValueError):
        EvidenceWriteRequest(expected_revision=0, records=[record, record])


def test_server_audit_fields_cannot_be_supplied_by_client():
    with pytest.raises(ValueError):
        EvidenceWriteRequest(expected_revision=0, records=[evidence(recorded_by="管理员")])


def test_missing_field_can_be_saved_but_cannot_be_frozen():
    request = EvidenceWriteRequest(expected_revision=0, records=[evidence(
        field_conflicts=[{"accident_field": "/不存在", "explanation": "合成冲突"}],
    )])
    records = audit_records(request.records, "owner")
    assert field_warnings(records, {"事实": "合成"})[0]["code"] == "unresolved_field"
    with pytest.raises(EvidenceBindingError) as error:
        freeze_snapshot({"事实": "合成"}, records, 1, "knowledge")
    assert error.value.warnings[0]["evidence_id"] == str(request.records[0].evidence_id)


def test_freezing_keeps_provenance_conflicts_and_unverified_status():
    first, second = evidence(), evidence()
    first["conflicts_with"] = [second["evidence_id"]]
    first["field_conflicts"] = [{"accident_field": "/a~1b/0", "explanation": "与字段不同"}]
    request = EvidenceWriteRequest(expected_revision=0, records=[first, second])
    records = audit_records(request.records, "owner")
    original = {"a/b": ["合成值"]}
    snapshot = freeze_snapshot(original, records, 1, "knowledge")
    records[0]["text"] = "修改不能污染快照"
    original["a/b"][0] = "修改"
    assert snapshot["accident_data"]["a/b"][0] == "合成值"
    frozen = snapshot["supplemental_records"][0]
    assert frozen["verification_status"] == "unverified"
    assert frozen["conflicts_with"] == [second["evidence_id"]]
    assert frozen["recorded_by"] == "owner"
    assert len(snapshot["fact_obligations"]) == 3
    assert "evidence:" + first["evidence_id"] in snapshot["source_digests"]
