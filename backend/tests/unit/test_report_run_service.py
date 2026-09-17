import asyncio
from uuid import uuid4

import pytest

from app.report_harness.errors import HarnessError
from app.schemas.report_run import CreateRunRequest
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import MemoryStore, SyntheticRoles, dependencies


def create(service):
    return service.create("owner", CreateRunRequest(
        request_id=uuid4(), session_id="synthetic-session",
        accident_data={"天气": "合成晴天"}, evidence_revision=0,
    ))


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
