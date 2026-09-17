import asyncio
from dataclasses import replace
from uuid import uuid4

import pytest

from app.report_harness.authorization import AuthorizationCatalog, AuthorizationRequest, EndpointDescription
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.schemas.report_run import CreateRunRequest
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles, dependencies


def service_with_catalog(store):
    catalog = AuthorizationCatalog([
        EndpointDescription(role=role, label="测试模型", base_url="https://example.invalid/v1",
                            model="synthetic", version="v1")
        for role in ("generator", "reviewer")
    ], [], frozenset({canonical_digest([])}))
    roles = SyntheticRoles()
    settings = replace(dependencies(roles, "outbound"),
                       endpoint_profile_digest=catalog.endpoint_digest,
                       knowledge_manifest_digest=catalog.knowledge_digest,
                       authorization_catalog=catalog)
    return ReportRunService(store, settings), roles


def test_authorization_is_owned_persistent_and_does_not_enable_unready_transport(pg_store):
    store, owner, other, session = pg_store
    service, roles = service_with_catalog(store)
    run = service.create(owner, CreateRunRequest(
        request_id=uuid4(), session_id=session, accident_data={"事实": "合成"}, evidence_revision=0,
    ))
    with pytest.raises(HarnessError, match="authorization_required"):
        asyncio.run(service.execute(owner, run["run_id"], 0))
    preview = service.authorization_preview(owner, run["run_id"])
    assert preview["available"]
    fields = ("snapshot_digest", "endpoint_profile_digest", "approved_knowledge_manifest_digest")
    request = AuthorizationRequest(**{key: preview[key] for key in fields}, confirmed=True)
    for name in fields:
        bad = request.model_copy(update={name: "0" * 64})
        with pytest.raises(HarnessError, match="authorization_digest_conflict"):
            service.authorize(owner, run["run_id"], bad)
    with pytest.raises(HarnessError) as error:
        service.authorize(other, run["run_id"], request)
    assert error.value.status_code == 404
    approved = service.authorize(owner, run["run_id"], request)
    assert approved["state_version"] == 1
    assert approved["budget"] == run["budget"]
    assert service.authorize(owner, run["run_id"], request) == approved
    restarted, _ = service_with_catalog(store)
    assert restarted.get(owner, run["run_id"]) == approved
    assert store.get(owner, run["run_id"])["approval"]["owner_user_id"] == owner
    with pytest.raises(HarnessError, match="outbound_transport_unavailable"):
        asyncio.run(service.execute(owner, run["run_id"], 1))
    assert roles.calls == []
