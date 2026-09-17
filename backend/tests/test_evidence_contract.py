from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.evidence import EvidenceWriteRequest
from app.report_harness.evidence_store import EvidenceStore


def evidence():
    return {
        "evidence_id": str(uuid4()), "text": "合成文字，来源为当事人陈述。",
        "source_label": "合成记录", "source_locator": "第一段", "kind": "statement",
        "verification_status": "unverified",
        "field_conflicts": [{"accident_field": "/未存在", "explanation": "合成冲突"}],
    }


def test_evidence_is_persistent_owner_scoped_and_cas_guarded(pg_store):
    store, owner, other, session = pg_store
    evidence_store = EvidenceStore(store)
    assert evidence_store.get(owner, session) == {"revision": 0, "records": []}
    payload = EvidenceWriteRequest(expected_revision=0, records=[evidence()])
    saved = evidence_store.save(owner, session, payload)
    assert saved["revision"] == 1
    assert saved["warnings"][0]["code"] == "unresolved_field"
    assert saved["records"][0]["recorded_by"] == owner
    with pytest.raises(HarnessError, match="evidence_revision_conflict"):
        evidence_store.save(owner, session, payload)
    with pytest.raises(HarnessError) as error:
        evidence_store.get(other, session)
    assert error.value.status_code == 404
    persisted = EvidenceStore(store).get(owner, session)
    assert persisted["records"] == saved["records"]
    assert persisted["revision"] == 1
    with store.connection() as conn:
        conn.execute("UPDATE chat_sessions SET draft_json=%s WHERE id=%s", ("{}", session))
    assert evidence_store.get(owner, session) == persisted


def test_two_connections_cannot_overwrite_the_same_evidence_revision(pg_store):
    store, owner, _, session = pg_store
    payload = EvidenceWriteRequest(expected_revision=0, records=[evidence()])

    def save(_):
        try:
            return EvidenceStore(store).save(owner, session, payload)["revision"]
        except HarnessError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    assert results.count(1) == 1
    assert results.count("evidence_revision_conflict") == 1


def test_evidence_http_route_obeys_revision_and_owner(pg_store):
    from fastapi import FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient
    from app.api.deps import get_current_user
    from app.api.routes_report_evidence import router
    from app.api.routes_report_runs import get_report_run_service
    from app.services.auth_service import AuthenticatedUser
    from app.services.report_run_service import ReportRunService
    from tests.harness_fixtures import SyntheticRoles, dependencies

    store, owner, other, session = pg_store
    service = ReportRunService(store, dependencies(SyntheticRoles()))
    app = FastAPI()
    app.include_router(router)

    def identity(authorization: str | None = Header(None)):
        identifier = {f"Bearer {owner}": owner, f"Bearer {other}": other}.get(authorization)
        if identifier is None:
            raise HTTPException(401)
        return AuthenticatedUser(id=identifier, username=identifier, display_name=None,
                                 role="user", is_active=True, created_at="", updated_at="")

    app.dependency_overrides[get_current_user] = identity
    app.dependency_overrides[get_report_run_service] = lambda: service
    path = f"/api/v1/chat-sessions/{session}/report-evidence"
    headers = {"Authorization": f"Bearer {owner}"}
    with TestClient(app) as client:
        assert client.get(path).status_code == 401
        assert client.get(path, headers=headers).json()["revision"] == 0
        payload = {"expected_revision": 0, "records": [evidence()]}
        saved = client.put(path, headers=headers, json=payload)
        assert saved.status_code == 200
        assert saved.json()["revision"] == 1
        assert client.put(path, headers=headers, json=payload).status_code == 409
        assert client.get(path, headers={"Authorization": f"Bearer {other}"}).status_code == 404


def test_evidence_snapshot_is_frozen_and_both_roles_receive_source_status(pg_store):
    import asyncio
    from app.schemas.report_run import CreateRunRequest
    from app.services.report_run_service import ReportRunService
    from tests.harness_fixtures import SyntheticRoles, dependencies

    class RecordingRoles(SyntheticRoles):
        def __init__(self):
            super().__init__()
            self.generator_snapshot = None

        async def generate(self, context):
            self.generator_snapshot = context["snapshot"]
            return await super().generate(context)

    store, owner, _, session = pg_store
    text = evidence()
    text["field_conflicts"] = []
    evidence_store = EvidenceStore(store)
    evidence_store.save(owner, session, EvidenceWriteRequest(expected_revision=0, records=[text]))
    roles = RecordingRoles()
    service = ReportRunService(store, dependencies(roles))
    run = service.create(owner, CreateRunRequest(
        request_id=uuid4(), session_id=session, accident_data={"事实": "合成"},
        evidence_revision=1,
    ))
    original_digest = run["snapshot_digest"]
    changed = {**text, "text": "第二次保存的新文字"}
    evidence_store.save(owner, session, EvidenceWriteRequest(expected_revision=1, records=[changed]))
    result = asyncio.run(service.execute(owner, run["run_id"], 0))
    assert result["state"] == "published"
    assert result["snapshot_digest"] == original_digest
    for snapshot in (roles.generator_snapshot, roles.review_context["snapshot"]):
        frozen = snapshot["supplemental_records"][0]
        assert frozen["text"] == text["text"]
        assert frozen["verification_status"] == "unverified"
        assert frozen["source_label"] == text["source_label"]
        assert frozen["recorded_by"] == owner
        assert snapshot["revision"] == 1


def test_stale_evidence_or_invalid_field_binding_prevents_create(pg_store):
    from app.schemas.report_run import CreateRunRequest
    from app.services.report_run_service import ReportRunService
    from tests.harness_fixtures import SyntheticRoles, dependencies

    store, owner, _, session = pg_store
    EvidenceStore(store).save(owner, session, EvidenceWriteRequest(expected_revision=0, records=[evidence()]))
    service = ReportRunService(store, dependencies(SyntheticRoles()))
    request = dict(request_id=uuid4(), session_id=session, accident_data={"事实": "合成"})
    with pytest.raises(HarnessError, match="evidence_revision_conflict"):
        service.create(owner, CreateRunRequest(**request, evidence_revision=0))
    with pytest.raises(HarnessError) as error:
        service.create(owner, CreateRunRequest(**request, evidence_revision=1))
    assert error.value.status_code == 422
    assert error.value.details["field_errors"][0]["accident_field"] == "/未存在"


def test_synthetic_review_finding_of_promoted_statement_prevents_publication(pg_store):
    import asyncio
    from app.schemas.report_run import CreateRunRequest
    from app.services.report_run_service import ReportRunService
    from tests.harness_fixtures import SyntheticRoles, dependencies

    class IncorrectCandidateRoles(SyntheticRoles):
        async def generate(self, context):
            result = await super().generate(context)
            result["report_markdown"] = "已核实当事人的陈述完全属实。"
            result["claims"][0]["text_span"]["end"] = len(result["report_markdown"])
            return result

        async def review(self, context):
            result = await super().review(context)
            source = context["snapshot"]["supplemental_records"][0]
            assert source["verification_status"] == "unverified"
            result["issues"] = [{
                "issue_id": "test-promoted-statement", "category": "facts", "severity": "major",
                "target": "已核实当事人的陈述完全属实。", "explanation": "合成审查问题：未核实陈述被提升。",
                "source_refs": ["evidence:" + source["evidence_id"]],
                "closure_condition": "保留陈述来源和未核实边界。", "status": "open",
            }]
            return result

    store, owner, _, session = pg_store
    record = evidence()
    record["field_conflicts"] = []
    EvidenceStore(store).save(owner, session, EvidenceWriteRequest(expected_revision=0, records=[record]))
    service = ReportRunService(store, dependencies(IncorrectCandidateRoles()))
    run = service.create(owner, CreateRunRequest(
        request_id=uuid4(), session_id=session, accident_data={"事实": "合成"}, evidence_revision=1,
    ))
    final = asyncio.run(service.execute(owner, run["run_id"], 0))
    assert final["state"] == "needs_review"
    assert "report" not in final
