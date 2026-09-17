import asyncio
from uuid import uuid4

import pytest

from app.schemas.report_run import CreateRunRequest
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles, dependencies


@pytest.mark.parametrize("invalid_review,expected", [(False, "published"), (True, "needs_review")])
def test_persisted_service_flow_uses_independent_roles(pg_store, invalid_review, expected):
    store, owner, _, session = pg_store
    roles = SyntheticRoles(invalid_review=invalid_review)
    service = ReportRunService(store, dependencies(roles))
    run = service.create(owner, CreateRunRequest(
        request_id=uuid4(), session_id=session, accident_data={"事实": "合成样本"},
        evidence_revision=0,
    ))
    assert roles.calls == []
    result = asyncio.run(service.execute(owner, run["run_id"], 0))
    assert result["state"] == expected
    assert roles.calls == ["prepare", "generate", "review"]
    assert roles.closed
    restarted = ReportRunService(store, dependencies(SyntheticRoles()))
    assert restarted.get(owner, run["run_id"]) == result
    candidate = restarted.candidate(owner, run["run_id"])
    assert candidate["candidate_version"] == 1
    assert candidate["review_result"]["candidate_digest"]
