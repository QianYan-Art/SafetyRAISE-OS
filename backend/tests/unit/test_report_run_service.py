import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.schemas.report_run import CreateRunRequest
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import (
    KnowledgeCitationRoles,
    MemoryStore,
    SyntheticRoles,
    dependencies,
)


def create(service):
    return service.create("owner", CreateRunRequest(
        request_id=uuid4(), session_id="synthetic-session",
        accident_data={"天气": "合成晴天"}, evidence_revision=0,
    ))


def legal_service(reviewer_read):
    roles = KnowledgeCitationRoles(reviewer_read)
    base = dependencies(roles)
    text = "合成法律原文：" + "法条正文。" * 7000
    source = {
        "id": "legal-rule", "document_id": "legal-doc", "version": "v1",
        "text": text, "digest": canonical_digest(text),
        "manifest_digest": base.knowledge_manifest_digest,
    }
    runtime = replace(base, knowledge_chunks=(source,))
    service = ReportRunService(MemoryStore(), runtime)
    return service, roles, create(service)


@pytest.mark.parametrize(
    ("reviewer_read", "expected_state"),
    [
        ("none", "needs_review"),
        ("partial", "needs_review"),
        ("id_only", "needs_review"),
        ("full", "published"),
    ],
)
def test_candidate_knowledge_refs_require_complete_independent_review(
    reviewer_read, expected_state,
):
    service, roles, run = legal_service(reviewer_read)

    result = asyncio.run(service.execute("owner", run["run_id"], 0))

    assert result["state"] == expected_state
    if expected_state == "needs_review":
        assert "report" not in result
    assert roles.closed


@pytest.mark.parametrize("external_source", [False, True])
def test_complete_excerpt_read_still_requires_revision_not_publication(external_source):
    roles = KnowledgeCitationRoles("full", "law#rule#0001")
    base = dependencies(roles)
    text = "合成规则摘录，即使读完也不是完整法条。"
    source = {
        "id": "law#rule#0001", "document_id": "law", "version": "v1",
        "text": text, "digest": canonical_digest(text),
        "manifest_digest": base.knowledge_manifest_digest,
    }
    runtime = replace(
        base, knowledge_chunks=(source,),
        external_knowledge_source=external_source,
        budget_policy=base.budget_policy.model_copy(update={"max_revision_rounds": 0}),
    )
    service = ReportRunService(MemoryStore(), runtime)
    run = create(service)
    result = asyncio.run(service.execute("owner", run["run_id"], 0))
    assert result["state"] == "needs_review"
    assert "report" not in result
    recorded = service.store.get("owner", run["run_id"])["review_history"][0]
    assert recorded["raw_review"]["issues"] == []
    assert recorded["review"]["issues"][0]["category"] == "citations"
    assert recorded["review"]["issues"][0]["status"] == "open"
    assert not next(
        check["passed"] for check in recorded["review"]["completed_checks"]
        if check["category"] == "citations"
    )


def test_controller_requires_separate_review_of_final_candidate():
    roles = SyntheticRoles()
    service = ReportRunService(MemoryStore(), dependencies(roles))
    run = create(service)
    assert roles.calls == []
    result = asyncio.run(service.execute("owner", run["run_id"], 0))
    assert result["state"] == "published"
    assert result["quality_gate"] == "engineering_only"
    assert not result["formal_export_eligible"]
    assert roles.calls == ["prepare", "generate", "review"]
    assert "prepared" not in roles.review_context
    assert roles.closed
    assert service.get("owner", run["run_id"]) == result


def test_invalid_review_cannot_publish():
    roles = SyntheticRoles(invalid_review=True)
    service = ReportRunService(MemoryStore(), dependencies(roles))
    run = create(service)
    result = asyncio.run(service.execute("owner", run["run_id"], 0))
    assert result["state"] == "needs_review"
    assert "report" not in result
    assert service.candidate("owner", run["run_id"])["display_status"] == "candidate"
    assert roles.closed


def test_revocation_after_review_is_checked_before_publication(monkeypatch):
    revoked = False

    class RevokingRoles(SyntheticRoles):
        async def review(self, context):
            nonlocal revoked
            result = await super().review(context)
            revoked = True
            object.__setattr__(service.dependencies, "production_outbound_enabled", True)
            return result

    roles = RevokingRoles()
    service = ReportRunService(MemoryStore(), dependencies(roles))
    run = create(service)
    # 合成角色验证控制流；不冒充正式模型或发布批准。
    validations = []

    def validate(*args):
        validations.append(revoked)
        if revoked:
            raise HarnessError("release_not_approved", 503)

    monkeypatch.setattr(service, "_validate_outbound_authorization", validate)
    with pytest.raises(HarnessError, match="release_not_approved"):
        asyncio.run(service.execute("owner", run["run_id"], 0))
    assert validations[-1] is True
    assert service.get("owner", run["run_id"])["state"] != "published"
    assert roles.closed


def test_unapproved_outbound_never_calls_roles():
    roles = SyntheticRoles()
    service = ReportRunService(MemoryStore(), dependencies(roles, "outbound"))
    run = create(service)
    with pytest.raises(HarnessError, match="authorization_required"):
        asyncio.run(service.execute("owner", run["run_id"], 0))
    assert roles.calls == []
    assert service.get("owner", run["run_id"])["state"] == "queued"


def test_role_failure_closes_resources_and_never_publishes():
    roles = SyntheticRoles(fail_generate=True)
    service = ReportRunService(MemoryStore(), dependencies(roles))
    run = create(service)
    with pytest.raises(RuntimeError):
        asyncio.run(service.execute("owner", run["run_id"], 0))
    assert service.get("owner", run["run_id"])["state"] == "failed"
    assert roles.closed


def test_cancelled_run_cannot_execute_again():
    roles = SyntheticRoles()
    service = ReportRunService(MemoryStore(), dependencies(roles))
    run = create(service)
    stopped = service.cancel("owner", run["run_id"])
    with pytest.raises(HarnessError, match="not_executable"):
        asyncio.run(service.execute("owner", run["run_id"], stopped["state_version"]))
    assert roles.calls == []


def test_non_json_accident_data_is_rejected_before_persistence():
    with pytest.raises(ValueError):
        CreateRunRequest(
            request_id=uuid4(), session_id="synthetic", evidence_revision=0,
            accident_data={"value": float("nan")},
        )


def test_oversized_candidate_is_not_truncated_into_a_publication():
    class OversizedRoles(SyntheticRoles):
        async def generate(self, context):
            result = await super().generate(context)
            result["report_markdown"] = "x" * (256 * 1024)
            return result

    roles = OversizedRoles()
    service = ReportRunService(MemoryStore(), dependencies(roles))
    run = create(service)
    result = asyncio.run(service.execute("owner", run["run_id"], 0))
    assert result["state"] == "needs_review"
    assert "report" not in result
    assert "review" not in roles.calls


def test_cancellation_after_claim_prevents_first_role_call():
    roles = SyntheticRoles()
    service = ReportRunService(MemoryStore(), dependencies(roles))
    run = create(service)
    token = service.claim("owner", run["run_id"], 0)
    service.cancel("owner", run["run_id"])
    with pytest.raises(HarnessError, match="lease_lost"):
        asyncio.run(service.execute_claimed("owner", run["run_id"], token))
    assert roles.calls == []


def test_old_worker_cannot_cancel_a_newer_execution_token():
    store = MemoryStore()
    service = ReportRunService(store, dependencies(SyntheticRoles()))
    run = create(service)
    token = service.claim("owner", run["run_id"], 0)
    store.tokens[run["run_id"]] += 1
    assert service.cancel_claimed("owner", run["run_id"], token)["state"] == "preparing"


def test_close_failure_does_not_replace_published_result(caplog):
    class CloseFailure(SyntheticRoles):
        async def close(self):
            raise RuntimeError("合成资源关闭故障")

    service = ReportRunService(MemoryStore(), dependencies(CloseFailure()))
    run = create(service)
    assert asyncio.run(service.execute("owner", run["run_id"], 0))["state"] == "published"
    assert "报告角色资源关闭失败" in caplog.text


class StaleCitationRoles(KnowledgeCitationRoles):
    """修订稿沿用上一版读过的知识引用；reread 时收到协议反馈后才重新读取。"""

    def __init__(self, reread):
        super().__init__("full")
        self.reread = reread
        self.generator_feedback = []

    async def generate(self, context):
        version = context["candidate_version"]
        if version > 1:
            self.generator_feedback.append(context.get("protocol_feedback"))
        if version == 1 or (self.reread and "protocol_feedback" in context):
            result = await super().generate(context)
        else:
            result = await SyntheticRoles.generate(self, context)
            result["claims"][0]["type"] = "knowledge"
            result["claims"][0]["knowledge_refs"] = [self.chunk_id]
        if version > 1 and "tool_calls" not in result:
            result["issue_responses"] = [{
                "issue_id": context["unresolved_issues"][0]["issue_id"], "action": "revised",
                "explanation": "合成修订：已按原文明确不确定性。", "source_refs": [self.chunk_id],
            }]
        return result

    async def review(self, context):
        result = await super().review(context)
        if "tool_calls" in result:
            return result
        if context["candidate"]["version"] == 1:
            result["issues"] = [{
                "issue_id": "model-proposed-id", "category": "reasoning", "severity": "major",
                "target": "claim-1", "explanation": "合成检查：需要明确不确定性。",
                "source_refs": context["snapshot"]["fact_obligations"][0]["source_refs"],
                "closure_condition": "明确给定事实不能支持确定因果。", "status": "open",
            }]
        else:
            issue = dict(context["unresolved_issues"][0])
            issue.update(status="resolved", explanation="合成复查：闭合条件满足。")
            result["issues"] = [issue]
        return result


def stale_citation_run(reread):
    roles = StaleCitationRoles(reread)
    base = dependencies(roles)
    text = "合成法律原文：转弯车辆应当让直行车辆先行。"
    source = {
        "id": roles.chunk_id, "document_id": "legal-doc", "version": "v1",
        "text": text, "digest": canonical_digest(text),
        "manifest_digest": base.knowledge_manifest_digest,
    }
    service = ReportRunService(MemoryStore(), replace(base, knowledge_chunks=(source,)))
    run = create(service)
    result = asyncio.run(service.execute("owner", run["run_id"], 0))
    return result, roles, service.store.get("owner", run["run_id"])


def test_revision_citing_previous_round_source_is_told_to_reread_instead_of_failing():
    result, roles, _record = stale_citation_run(reread=True)
    assert result["state"] == "published", result["terminal_reason"]
    assert result["candidate_version"] == 2
    first, retry = roles.generator_feedback[0], roles.generator_feedback[1]
    assert first is None
    errors = retry["repairs"][0]["errors"]
    assert {tuple(item["path"]) for item in errors} == {
        ("claims", 0, "knowledge_refs"), ("issue_responses", 0, "source_refs"),
    }
    assert all(item["type"] == "source_not_read" and roles.chunk_id in item["message"]
               for item in errors)


def test_revision_that_never_rereads_stops_with_recorded_reason():
    result, _roles, record = stale_citation_run(reread=False)
    assert result["state"] == "needs_review"
    assert result["terminal_reason"] == "invalid_role_response"
    assert "report" not in result
    detail = record["terminal_detail"]
    assert detail["kind"] == "response_rejected"
    assert all(item["type"] == "source_not_read" and "legal-rule" in item["message"]
               for item in detail["errors"])


class UnsourcedClaimRoles(SyntheticRoles):
    """首稿含一条无来源断言；fix 时按协议反馈补上来源。"""

    def __init__(self, fix):
        super().__init__()
        self.fix = fix
        self.generator_feedback = []

    async def generate(self, context):
        self.generator_feedback.append(context.get("protocol_feedback"))
        result = await super().generate(context)
        if not (self.fix and "protocol_feedback" in context):
            result["claims"][0]["evidence_refs"] = []
        return result


@pytest.mark.parametrize("fix", [True, False])
def test_candidate_contract_is_repaired_before_paying_for_review(fix):
    roles = UnsourcedClaimRoles(fix)
    service = ReportRunService(MemoryStore(), dependencies(roles))
    run = create(service)
    result = asyncio.run(service.execute("owner", run["run_id"], 0))
    feedback = roles.generator_feedback[1]["repairs"][0]["errors"]
    assert feedback == [{"type": "candidate_contract", "path": ["claims", 0],
                         "message": "断言 claim-1 缺少来源引用。"}]
    if fix:
        assert result["state"] == "published", result["terminal_reason"]
        assert roles.calls == ["prepare", "generate", "generate", "review"]
    else:
        assert result["state"] == "needs_review"
        assert result["terminal_reason"] == "invalid_role_response"
        assert roles.calls == ["prepare", "generate", "generate", "generate"]
        detail = service.store.get("owner", run["run_id"])["terminal_detail"]
        assert detail["kind"] == "response_rejected"
        assert detail["errors"][0]["type"] == "candidate_contract"
