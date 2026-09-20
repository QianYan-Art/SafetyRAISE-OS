from copy import deepcopy

from app.report_harness.contracts import canonical_digest, enforce_source_authority
from app.report_harness.review_ledger import IssueLedger
from app.schemas.report_run import CandidateReport, ReviewResult


def sample(ref="law#rule#0001"):
    candidate = CandidateReport.model_validate({
        "version": 1, "report_markdown": "合成责任原则，仅验证引用控制。",
        "claims": [{
            "claim_id": "law", "type": "knowledge", "text_span": {"start": 0, "end": 4},
            "evidence_refs": [], "knowledge_refs": [ref],
        }],
        "obligation_resolutions": [],
    })
    review = ReviewResult.model_validate({
        "candidate_digest": canonical_digest(candidate), "snapshot_digest": "a" * 64,
        "issues": [], "coverage_checks": [],
        "completed_checks": [{
            "category": category, "passed": True, "conclusion": "合成通过结论。",
            "evidence_refs": [], "knowledge_refs": [ref],
        } for category in ("facts", "coverage", "reasoning", "citations", "conciseness")],
    })
    return candidate, review


def test_rule_excerpt_cannot_pass_by_disclaimer_or_model_approval():
    candidate, review = sample()
    raw = review.model_dump()
    result = enforce_source_authority(
        candidate, review, [{"id": "law#rule#0001", "source_kind": "rule_excerpt"}], [],
    )
    assert review.model_dump() == raw
    assert result.issues[0].severity == "major"
    assert result.issues[0].status == "open"
    assert not next(c for c in result.completed_checks if c.category == "citations").passed


def test_structured_kind_does_not_depend_on_rule_id_spelling():
    candidate, review = sample("arbitrary-id")
    result = enforce_source_authority(
        candidate, review, [{"id": "arbitrary-id", "source_kind": "rule_excerpt"}], [],
    )
    assert result.issues[0].source_refs == ["arbitrary-id"]


def test_old_rule_identifier_is_not_grandfathered_as_full_source():
    candidate, review = sample()
    result = enforce_source_authority(candidate, review, [{"id": "law#rule#0001"}], [])
    assert result.issues


def test_body_reference_does_not_get_an_automatic_applicability_approval():
    candidate, review = sample("law#0001")
    review.completed_checks[3].passed = False
    result = enforce_source_authority(
        candidate, review, [{"id": "law#0001", "source_kind": "source_chunk"}], [],
    )
    assert result is review
    assert not result.completed_checks[3].passed


def test_unfixed_rule_issue_keeps_stable_identity_and_rejects_false_closure():
    candidate, review = sample()
    sources = [{"id": "law#rule#0001", "source_kind": "rule_excerpt"}]
    ledger = IssueLedger()
    initial = ledger.apply(enforce_source_authority(candidate, review, sources, []))
    original = initial.issues[0]
    followup = initial.model_copy(deep=True)
    followup.issues[0].status = "resolved"
    followup.issues[0].explanation = "合成误报：只加免责声明即可关闭。"
    corrected = enforce_source_authority(candidate, followup, sources, ledger.history())
    actual = ledger.apply(corrected)
    assert actual.issues[0].issue_id == original.issue_id
    assert actual.issues[0].closure_condition == original.closure_condition
    assert actual.issues[0].status == "open"


def test_fixed_source_is_left_to_independent_reviewer_to_close():
    candidate, review = sample()
    sources = [{"id": "law#rule#0001", "source_kind": "rule_excerpt"}]
    ledger = IssueLedger()
    initial = ledger.apply(enforce_source_authority(candidate, review, sources, []))
    fixed = candidate.model_copy(deep=True)
    fixed.claims[0].knowledge_refs = ["law#0001"]
    followup = initial.model_copy(deep=True)
    followup.issues[0].explanation = "已独立读取合成完整来源并核实适用范围。"
    followup.issues[0].source_refs = ["law#0001"]
    followup.issues[0].status = "resolved"
    before = deepcopy(followup)
    checked = enforce_source_authority(fixed, followup, sources, ledger.history())
    assert checked == before
    assert ledger.apply(checked).issues[0].status == "resolved"
