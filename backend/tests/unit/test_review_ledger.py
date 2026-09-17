from __future__ import annotations

from copy import deepcopy
from uuid import UUID

import pytest

from app.report_harness.review_ledger import IssueLedger
from app.schemas.report_run import IssueResponse, ReviewResult


def issue(
    *,
    issue_id: str = "model-issue-1",
    category: str = "facts",
    severity: str = "major",
    target: str = "事实结论",
    explanation: str = "当前候选没有充分说明该事实。",
    source_refs: list[str] | None = None,
    closure_condition: str = "补充可核验来源并说明其支持范围。",
    status: str = "open",
) -> dict:
    return {
        "issue_id": issue_id,
        "category": category,
        "severity": severity,
        "target": target,
        "explanation": explanation,
        "source_refs": [] if source_refs is None else source_refs,
        "closure_condition": closure_condition,
        "status": status,
    }


def review(*issues: dict) -> ReviewResult:
    return ReviewResult.model_validate({
        "candidate_digest": "candidate-digest",
        "snapshot_digest": "snapshot-digest",
        "coverage_checks": [],
        "issues": list(issues),
        "completed_checks": [],
    })


def dumped(issue_model) -> dict:
    return issue_model.model_dump(mode="json")


def test_initial_issues_receive_stable_program_ids_and_strict_copy():
    incoming = review(issue(issue_id="model-id"))
    ledger = IssueLedger()

    result = ledger.apply(incoming)

    assert result is not incoming
    assert result.issues[0] is not incoming.issues[0]
    assert result.issues[0].issue_id != "model-id"
    UUID(result.issues[0].issue_id)
    assert incoming.issues[0].issue_id == "model-id"
    assert ledger.unresolved() == [dumped(result.issues[0])]


def test_first_or_new_issue_cannot_be_resolved_and_duplicate_ids_are_rejected():
    with pytest.raises(ValueError):
        IssueLedger().apply(review(issue(status="resolved", source_refs=["evidence:x"])))

    ledger = IssueLedger()
    first = ledger.apply(review(issue(issue_id="same")))
    stable_id = first.issues[0].issue_id
    with pytest.raises(ValueError):
        ledger.apply(review(
            issue(issue_id=stable_id),
            issue(issue_id=stable_id, target="另一个目标"),
        ))

    with pytest.raises(ValueError):
        IssueLedger().apply(review(
            issue(issue_id="duplicate"),
            issue(issue_id="duplicate", target="另一个目标"),
        ))


def test_follow_up_must_explicitly_include_all_unresolved_issues():
    ledger = IssueLedger()
    ledger.apply(review(issue()))

    with pytest.raises(ValueError, match="未关闭问题"):
        ledger.apply(review())


@pytest.mark.parametrize("field, value", [
    ("category", "coverage"),
    ("severity", "minor"),
    ("closure_condition", "改写为另一闭合条件。"),
])
def test_known_issue_locked_fields_cannot_change(field: str, value: str):
    ledger = IssueLedger()
    first = ledger.apply(review(issue()))
    stable_id = first.issues[0].issue_id
    changed = issue(issue_id=stable_id, explanation="新的解释。")
    changed[field] = value

    with pytest.raises(ValueError):
        ledger.apply(review(changed))


def test_unresolved_issue_cannot_be_reidentified_with_a_new_id():
    ledger = IssueLedger()
    ledger.apply(review(issue(issue_id="model-first")))

    with pytest.raises(ValueError):
        ledger.apply(review(issue(issue_id="model-replacement")))


def test_resolution_requires_evidence_and_a_new_closure_explanation():
    ledger = IssueLedger()
    first = ledger.apply(review(issue(explanation="原始问题解释。")))
    stable_id = first.issues[0].issue_id

    with pytest.raises(ValueError, match="source_refs"):
        ledger.apply(review(issue(
            issue_id=stable_id,
            explanation="新的闭合解释。",
            status="resolved",
        )))
    with pytest.raises(ValueError, match="区别于旧解释"):
        ledger.apply(review(issue(
            issue_id=stable_id,
            explanation="原始问题解释。",
            source_refs=["evidence:confirmed"],
            status="resolved",
        )))

    resolved = ledger.apply(review(issue(
        issue_id=stable_id,
        explanation="已核对来源并补足候选中的缺口。",
        source_refs=["evidence:confirmed"],
        status="resolved",
    )))
    assert resolved.issues[0].issue_id == stable_id
    assert ledger.unresolved() == []
    assert ledger.resolved_ids() == {stable_id}


def test_history_is_append_only_and_return_values_are_deep_copies():
    ledger = IssueLedger()
    first = ledger.apply(review(issue(explanation="第一次解释。")))
    stable_id = first.issues[0].issue_id
    ledger.apply(review(issue(
        issue_id=stable_id,
        explanation="第二次解释。",
        status="contested",
    )))

    history = ledger.history()
    assert len(history) == 2
    assert history[0]["explanation"] == "第一次解释。"
    history[0]["explanation"] = "篡改历史"
    history.append({"issue_id": "伪造"})
    unresolved = ledger.unresolved()
    unresolved[0]["explanation"] = "篡改当前"
    assert len(ledger.history()) == 2
    assert ledger.history()[0]["explanation"] == "第一次解释。"
    assert ledger.unresolved()[0]["explanation"] == "第二次解释。"


def test_resolved_issue_may_be_omitted_or_explicitly_reopened():
    ledger = IssueLedger()
    first = ledger.apply(review(issue(explanation="待核对。")))
    stable_id = first.issues[0].issue_id
    ledger.apply(review(issue(
        issue_id=stable_id,
        explanation="已依据来源完成闭合。",
        source_refs=["evidence:confirmed"],
        status="resolved",
    )))

    ledger.apply(review())
    assert ledger.resolved_ids() == {stable_id}
    assert len(ledger.history()) == 2

    reopened = ledger.apply(review(issue(
        issue_id=stable_id,
        explanation="新增反驳使该问题需要重新检查。",
        status="open",
    )))
    assert reopened.issues[0].issue_id == stable_id
    assert ledger.resolved_ids() == set()
    assert [item["issue_id"] for item in ledger.unresolved()] == [stable_id]
    assert len(ledger.history()) == 3


def test_new_issue_on_follow_up_gets_a_fresh_program_id():
    ledger = IssueLedger()
    first = ledger.apply(review(issue(issue_id="model-first")))
    stable_id = first.issues[0].issue_id

    result = ledger.apply(review(
        issue(issue_id=stable_id),
        issue(issue_id="model-second", target="新增事实"),
    ))
    assert result.issues[0].issue_id == stable_id
    assert result.issues[1].issue_id != "model-second"
    UUID(result.issues[1].issue_id)
    assert len({item.issue_id for item in result.issues}) == 2


def test_apply_does_not_mutate_review_or_ledger_snapshots():
    original = review(issue(issue_id="model-id"))
    before = deepcopy(original.model_dump(mode="json"))
    ledger = IssueLedger()
    result = ledger.apply(original)
    result.issues[0].explanation = "修改返回值"

    assert original.model_dump(mode="json") == before
    assert ledger.history()[0]["explanation"] == "当前候选没有充分说明该事实。"


def response(issue_id: str, *, action: str = "revised", explanation: str = "已按来源修订候选。") -> IssueResponse:
    return IssueResponse(
        issue_id=issue_id,
        action=action,
        explanation=explanation,
        source_refs=["evidence:response-source"],
    )


def test_record_responses_accepts_only_current_unresolved_ids_and_appends_generator_history():
    ledger = IssueLedger()
    first = ledger.apply(review(issue()))
    stable_id = first.issues[0].issue_id

    ledger.record_responses([response(stable_id)])
    assert ledger.unresolved()[0]["status"] == "open"
    history = ledger.history()
    assert history[-1] == {
        "record_type": "issue_response",
        "role": "generator",
        "issue_id": stable_id,
        "action": "revised",
        "explanation": "已按来源修订候选。",
        "source_refs": ["evidence:response-source"],
    }
    assert ledger.unresolved()[0]["category"] == "facts"
    assert ledger.unresolved()[0]["severity"] == "major"
    assert ledger.unresolved()[0]["closure_condition"] == "补充可核验来源并说明其支持范围。"

    ledger.record_responses([response(
        stable_id,
        action="contested",
        explanation="生成者基于同一来源提出可核查反驳。",
    )])
    assert ledger.unresolved()[0]["status"] == "contested"
    assert ledger.history()[-1]["role"] == "generator"
    assert ledger.history()[-1]["action"] == "contested"


def test_record_responses_rejects_duplicate_unknown_resolved_and_non_model_inputs_atomically():
    ledger = IssueLedger()
    first = ledger.apply(review(issue()))
    stable_id = first.issues[0].issue_id
    before = deepcopy(ledger.history())

    with pytest.raises(ValueError):
        ledger.record_responses([response(stable_id), response(stable_id)])
    assert ledger.history() == before

    with pytest.raises(ValueError):
        ledger.record_responses([response("unknown")])
    assert ledger.history() == before

    ledger.apply(review(issue(
        issue_id=stable_id,
        explanation="已关闭。",
        source_refs=["evidence:closure"],
        status="resolved",
    )))
    before_closed = deepcopy(ledger.history())
    with pytest.raises(ValueError):
        ledger.record_responses([response(stable_id, action="contested")])
    assert ledger.history() == before_closed

    with pytest.raises(ValueError):
        ledger.record_responses([{"issue_id": stable_id}])


def test_record_responses_does_not_close_issue_and_contested_remains_for_reviewer():
    ledger = IssueLedger()
    stable_id = ledger.apply(review(issue())).issues[0].issue_id
    ledger.record_responses([response(
        stable_id,
        action="contested",
        explanation="回应无法替代独立审查。",
    )])

    assert ledger.resolved_ids() == set()
    assert ledger.unresolved() == [
        {
            "issue_id": stable_id,
            "category": "facts",
            "severity": "major",
            "target": "事实结论",
            "explanation": "当前候选没有充分说明该事实。",
            "source_refs": [],
            "closure_condition": "补充可核验来源并说明其支持范围。",
            "status": "contested",
        },
    ]
