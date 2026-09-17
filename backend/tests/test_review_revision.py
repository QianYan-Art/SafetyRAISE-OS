import pytest

from app.report_harness.contracts import canonical_digest
from tests.harness_fixtures import SyntheticRoles
from tests.test_report_tool_policy import run_http, tool_client


class RevisionRoles(SyntheticRoles):
    """预编排的问题与闭合响应，仅验证控制流，不模拟真实语义能力。"""

    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.versions = []
        self.reviewed = []
        self.received_issues = []

    async def generate(self, context):
        self.versions.append(context["candidate_version"])
        result = await super().generate(context)
        if context["candidate_version"] > 1:
            result["report_markdown"] += "不确定性已明确。"
            if self.mode in {"contested", "generator_closes"}:
                result["issue_responses"] = [{
                    "issue_id": context["unresolved_issues"][0]["issue_id"],
                    "action": "resolved" if self.mode == "generator_closes" else "contested",
                    "explanation": "合成反驳：给定来源只支持不确定性，不要求补造因果。",
                    "source_refs": context["snapshot"]["fact_obligations"][0]["source_refs"],
                }]
            if self.mode == "negative_check":
                assert not context["review_feedback"]["completed_checks"][0]["passed"]
        return result

    async def review(self, context):
        version = context["candidate"]["version"]
        self.reviewed.append(version)
        self.received_issues.append(context["unresolved_issues"])
        result = await super().review(context)
        refs = context["snapshot"]["fact_obligations"][0]["source_refs"]
        if self.mode == "negative_check":
            result["completed_checks"][0]["passed"] = version > 1
            return result
        if version == 1:
            result["issues"] = [{
                "issue_id": "model-proposed-id",
                "category": "style" if self.mode == "minor" else "reasoning",
                "severity": "minor" if self.mode == "minor" else "major",
                "target": "claim-1", "explanation": "合成检查：需要明确不确定性。",
                "source_refs": refs, "closure_condition": "明确给定事实不能支持确定因果。",
                "status": "resolved" if self.mode == "initial_resolved" else "open",
            }]
            return result
        if self.mode == "dropped":
            return result
        issue = dict(context["unresolved_issues"][0])
        if self.mode == "downgrade":
            issue["severity"] = "minor"
            issue["category"] = "style"
        elif self.mode == "persistent":
            issue["status"] = "contested"
            issue["explanation"] = "合成复查：重大问题仍未消除。"
        else:
            issue["status"] = "resolved"
            issue["explanation"] = "合成复查：本版本已明确不确定性，闭合条件满足。"
        if self.mode == "retarget":
            issue["target"] = "unrelated-claim"
        result["issues"] = [issue]
        if self.mode == "wrong_digest":
            result["candidate_digest"] = canonical_digest({"not": "final-candidate"})
        return result


def test_http_revision_is_independently_reviewed_before_publication(tool_client):
    roles = RevisionRoles("fixed")
    record, _ = run_http(tool_client, roles)
    assert record["state"] == "published", record["terminal_reason"]
    assert roles.versions == [1, 2] and roles.reviewed == [1, 2]
    assert roles.received_issues[1][0]["issue_id"] != "model-proposed-id"
    assert record["candidate_version"] == 2
    assert record["publication"]["candidate_digest"] == canonical_digest(record["candidate"])
    assert record["review"]["candidate_digest"] == record["publication"]["candidate_digest"]
    assert len(record["issue_history"]) >= 2
    assert [item["version"] for item in record["candidate_history"]] == [1, 2]
    for candidate, review in zip(record["candidate_history"], record["review_history"]):
        assert candidate["digest"] == review["candidate_digest"]
        assert candidate["digest"] == canonical_digest(candidate["candidate"])


@pytest.mark.parametrize("mode", [
    "dropped", "downgrade", "wrong_digest", "initial_resolved", "generator_closes", "retarget",
])
def test_http_revision_cannot_erase_or_self_close_review_findings(tool_client, mode):
    record, _ = run_http(tool_client, RevisionRoles(mode))
    assert record["state"] == "needs_review"
    assert "report" not in record


def test_http_two_revision_rounds_exhausted_retains_candidate_and_issues(tool_client):
    roles = RevisionRoles("persistent")
    record, _ = run_http(tool_client, roles)
    assert record["state"] == "needs_review"
    assert record["terminal_reason"] == "revision_rounds_exhausted"
    assert roles.versions == [1, 2, 3] and roles.reviewed == [1, 2, 3]
    assert record["candidate_version"] == 3
    assert record["review"]["issues"][0]["status"] == "contested"
    assert not record["formal_export_eligible"]


def test_http_only_minor_wording_issue_does_not_force_revision(tool_client):
    roles = RevisionRoles("minor")
    record, _ = run_http(tool_client, roles)
    assert record["state"] == "published"
    assert roles.versions == [1]
    assert record["review"]["issues"][0]["severity"] == "minor"


def test_http_evidenced_contest_requires_independent_closure(tool_client):
    roles = RevisionRoles("contested")
    record, _ = run_http(tool_client, roles)
    assert record["state"] == "published", record["terminal_reason"]
    assert roles.received_issues[1][0]["status"] == "contested"
    assert record["review"]["issues"][0]["status"] == "resolved"
    assert record["candidate"]["issue_responses"][0]["action"] == "contested"


def test_http_negative_global_check_is_not_lost_in_revision_feedback(tool_client):
    roles = RevisionRoles("negative_check")
    record, _ = run_http(tool_client, roles)
    assert record["state"] == "published"
    assert roles.reviewed == [1, 2]
