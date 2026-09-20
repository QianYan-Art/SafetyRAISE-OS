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
