from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.report_harness.contracts import canonical_digest, validate_publication
from app.schemas.report_run import (
    BudgetPolicy,
    CandidateReport,
    Claim,
    CompletedCheck,
    CoverageCheck,
    CreateRunRequest,
    EvidenceRecord,
    ExecuteRunRequest,
    Issue,
    ObligationResolution,
    ReviewResult,
)


SNAPSHOT_DIGEST = canonical_digest({"snapshot": "synthetic"})
OBLIGATION_ID = "fact:/facts/observed"
EVIDENCE_ID = "accident:/facts/observed"
KNOWLEDGE_ID = "knowledge:traffic-rule-1"


def make_candidate(**updates: object) -> CandidateReport:
    report = "事实已观察，原因仍需结合证据判断。"
    values: dict[str, object] = {
        "version": 1,
        "report_markdown": report,
        "claims": [
            {
                "claim_id": "claim-1",
                "text_span": {"start": 0, "end": 6},
                "type": "fact",
                "evidence_refs": [EVIDENCE_ID],
            }
        ],
        "obligation_resolutions": [
            {
                "obligation_id": OBLIGATION_ID,
                "treatment": "covered",
                "resolution": "正文明确覆盖该必要事实。",
            }
        ],
    }
    values.update(updates)
    return CandidateReport.model_validate(values)


def make_review(candidate: CandidateReport, **updates: object) -> ReviewResult:
    checks = [
        {
            "category": category,
            "passed": True,
            "evidence_refs": [EVIDENCE_ID],
            "conclusion": f"已完成 {category} 检查并记录具体依据。",
        }
        for category in ("facts", "coverage", "reasoning", "citations", "conciseness")
    ]
    values: dict[str, object] = {
        "candidate_digest": canonical_digest(candidate.model_dump(mode="json")),
        "snapshot_digest": SNAPSHOT_DIGEST,
        "coverage_checks": [
            {
                "obligation_id": OBLIGATION_ID,
                "passed": True,
                "claim_ids": ["claim-1"],
                "evidence_refs": [EVIDENCE_ID],
                "conclusion": "必要事实由候选断言覆盖。",
            }
        ],
        "issues": [],
        "completed_checks": checks,
    }
    values.update(updates)
    return ReviewResult.model_validate(values)


def publish(candidate: CandidateReport | None = None, review: ReviewResult | None = None) -> None:
    candidate = candidate or make_candidate()
    review = review or make_review(candidate)
    validate_publication(
        candidate,
        review,
        SNAPSHOT_DIGEST,
        {OBLIGATION_ID},
        {EVIDENCE_ID},
        {KNOWLEDGE_ID},
    )


def test_canonical_digest_is_order_independent_and_json_based() -> None:
    assert canonical_digest({"b": 2, "a": 1}) == canonical_digest({"a": 1, "b": 2})
    candidate = make_candidate()
    assert canonical_digest(candidate) == canonical_digest(candidate.model_dump(mode="json"))


def test_create_and_execute_requests_are_strict_and_bounded() -> None:
    request = CreateRunRequest(
        request_id=uuid4(),
        session_id=" session-1 ",
        accident_data={"facts": {"observed": True}},
        evidence_revision=0,
    )
    assert request.session_id == "session-1"
    with pytest.raises(ValidationError):
        CreateRunRequest(
            request_id=uuid4(),
            session_id="session-1",
            accident_data={},
            evidence_revision=0,
        )
    with pytest.raises(ValidationError):
        CreateRunRequest(
            request_id=uuid4(),
            session_id="session-1",
            accident_data={"facts": True},
            evidence_revision=-1,
        )
    with pytest.raises(ValidationError):
        CreateRunRequest(
            request_id=uuid4(),
            session_id="session-1",
            accident_data={"facts": True},
            evidence_revision=0,
            unexpected=True,
        )
    assert ExecuteRunRequest(expected_version=0).expected_version == 0
    with pytest.raises(ValidationError):
        ExecuteRunRequest(expected_version=-1)


def test_evidence_record_accepts_only_human_fields_and_checks_conflicts() -> None:
    evidence_id = uuid4()
    assert EvidenceRecord(
        evidence_id=evidence_id,
        text="  现场可见车辆停止。 ",
        source_label="用户确认",
        source_locator="视频 00:12",
        kind="observation",
        verification_status="human_confirmed",
        verification_note="由用户逐帧确认。",
    ).text == "现场可见车辆停止。"
    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id=evidence_id,
            text="事实",
            source_label="用户",
            source_locator="页 1",
            kind="statement",
            verification_status="human_confirmed",
        )
    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id=evidence_id,
            text="事实",
            source_label="用户",
            source_locator="页 1",
            kind="statement",
            verification_status="unverified",
            conflicts_with=[evidence_id],
        )
    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id=evidence_id,
            text="事实",
            source_label="用户",
            source_locator="https://example.invalid/source",
            kind="statement",
            verification_status="unverified",
        )
    with pytest.raises(ValidationError):
        EvidenceRecord(
            evidence_id=evidence_id,
            text="事实",
            source_label="用户",
            source_locator="页 1",
            kind="statement",
            verification_status="unverified",
            recorded_by="forged-user",
        )


def test_budget_policy_matches_development_defaults_and_sub_limit() -> None:
    policy = BudgetPolicy()
    assert policy.max_physical_requests == 24
    assert policy.max_tool_calls == 24
    assert policy.max_revision_rounds == 2
    assert policy.max_retrieval_requests == 8
    assert policy.max_active_seconds == 600
    assert policy.max_total_tokens == 120000
    assert policy.max_output_tokens_per_request == 8192
    assert policy.max_money is None
    with pytest.raises(ValidationError):
        BudgetPolicy(max_physical_requests=2, max_retrieval_requests=3)
    with pytest.raises(ValidationError):
        BudgetPolicy(max_total_tokens=-1)


def test_valid_candidate_and_review_can_pass_deterministically() -> None:
    publish()


def test_publication_rejects_digest_mismatch() -> None:
    candidate = make_candidate()
    review = make_review(candidate, candidate_digest="0" * 64)
    with pytest.raises(ValueError, match="candidate_digest"):
        publish(candidate, review)

    review = make_review(candidate, snapshot_digest="1" * 64)
    with pytest.raises(ValueError, match="snapshot_digest"):
        publish(candidate, review)


def test_publication_requires_all_five_concrete_checks() -> None:
    candidate = make_candidate()
    checks = [
        {
            "category": category,
            "passed": True,
            "evidence_refs": [EVIDENCE_ID],
            "conclusion": "有具体来源和结论。",
        }
        for category in ("facts", "coverage", "reasoning", "citations")
    ]
    review = make_review(candidate, completed_checks=checks)
    with pytest.raises(ValueError, match="五类审查检查"):
        publish(candidate, review)

    checks.append(
        {
            "category": "conciseness",
            "passed": False,
            "evidence_refs": [EVIDENCE_ID],
            "conclusion": "发现需要重写。",
        }
    )
    review = make_review(candidate, completed_checks=checks)
    with pytest.raises(ValueError, match="未通过"):
        publish(candidate, review)

    empty_basis = [
        {
            "category": category,
            "passed": True,
            "evidence_refs": [] if category == "facts" else [EVIDENCE_ID],
            "conclusion": "有具体结论。",
        }
        for category in ("facts", "coverage", "reasoning", "citations", "conciseness")
    ]
    review = make_review(candidate, completed_checks=empty_basis)
    with pytest.raises(ValueError, match="具体来源依据"):
        publish(candidate, review)


def test_publication_requires_complete_obligations_and_in_scope_references() -> None:
    candidate = make_candidate(obligation_resolutions=[])
    review = make_review(candidate)
    with pytest.raises(ValueError, match="义务处置"):
        publish(candidate, review)

    candidate = make_candidate()
    review = make_review(candidate, coverage_checks=[])
    with pytest.raises(ValueError, match="coverage 检查不完整"):
        publish(candidate, review)

    candidate = make_candidate(
        claims=[
            {
                "claim_id": "claim-1",
                "text_span": {"start": 0, "end": 6},
                "type": "fact",
                "evidence_refs": ["accident:/other"],
            }
        ]
    )
    review = make_review(candidate)
    with pytest.raises(ValueError, match="未授权"):
        publish(candidate, review)

    candidate = make_candidate()
    review = make_review(
        candidate,
        issues=[
            {
                "issue_id": "issue-1",
                "category": "citation",
                "severity": "major",
                "target": "claim-1",
                "explanation": "来源不支持该结论。",
                "source_refs": ["accident:/unknown"],
                "closure_condition": "补充可核验来源。",
                "status": "open",
            }
        ],
    )
    with pytest.raises(ValueError, match="越界来源引用"):
        publish(candidate, review)


def test_publication_rejects_unresolved_major_blocker_and_bad_body_location() -> None:
    candidate = make_candidate()
    review = make_review(
        candidate,
        issues=[
            {
                "issue_id": "issue-1",
                "category": "fact",
                "severity": "major",
                "target": "claim-1",
                "explanation": "必要事实未被支持。",
                "source_refs": [EVIDENCE_ID],
                "closure_condition": "重新核对事实。",
                "status": "contested",
            }
        ],
    )
    with pytest.raises(ValueError, match="未关闭"):
        publish(candidate, review)

    candidate = make_candidate(
        claims=[
            {
                "claim_id": "claim-1",
                "text_span": {"start": 0, "end": 999},
                "type": "fact",
                "evidence_refs": [EVIDENCE_ID],
            }
        ]
    )
    review = make_review(candidate)
    with pytest.raises(ValueError, match="正文范围"):
        publish(candidate, review)


def test_review_models_reject_extra_fields() -> None:
    candidate = make_candidate()
    with pytest.raises(ValidationError):
        ReviewResult(
            candidate_digest=canonical_digest(candidate.model_dump(mode="json")),
            snapshot_digest=SNAPSHOT_DIGEST,
            completed_checks=[],
            extra_field=True,
        )


def test_first_review_cannot_declare_resolved_and_semantic_minor_is_rejected() -> None:
    candidate = make_candidate()
    resolved_issue = {
        "issue_id": "issue-major",
        "category": "fact",
        "severity": "major",
        "target": "claim-1",
        "explanation": "曾发现问题，已修复。",
        "source_refs": [EVIDENCE_ID],
        "closure_condition": "已由最终候选修复。",
        "status": "resolved",
    }
    with pytest.raises(ValueError, match="首次声明 resolved"):
        publish(candidate, make_review(candidate, issues=[resolved_issue]))

    for category in ("fact", "inference", "citation"):
        minor_issue = {
            "issue_id": f"issue-{category}",
            "category": category,
            "severity": "minor",
            "target": "claim-1",
            "explanation": "语义问题不能降级。",
            "source_refs": [EVIDENCE_ID],
            "closure_condition": "补足事实、推理或引用支持。",
            "status": "open",
        }
        with pytest.raises(ValueError, match="不能标记为 minor"):
            publish(candidate, make_review(candidate, issues=[minor_issue]))

    style_issue = {
        "issue_id": "issue-style",
        "category": "style",
        "severity": "minor",
        "target": "claim-1",
        "explanation": "措辞可微调。",
        "source_refs": [],
        "closure_condition": "可选文字润色。",
        "status": "open",
    }
    publish(candidate, make_review(candidate, issues=[style_issue]))
