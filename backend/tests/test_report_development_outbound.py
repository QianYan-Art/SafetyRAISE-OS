from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from app.report_harness.authorization import AuthorizationCatalog, AuthorizationRequest, EndpointDescription
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.execution import ReportExecutionDependencies
from app.schemas.report_run import CreateRunRequest
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import MemoryStore, SyntheticRoles, dependencies

_MISSING = object()


class AuthorizableMemoryStore(MemoryStore):
    @contextmanager
    def locked(self, owner, run_id):
        document = self.get(owner, run_id)
        yield None, {"owner": owner, "run_id": run_id, "state": document["state"],
                     "document": document}

    def _view(self, row):
        return self.get(row["owner"], row["run_id"])

    def save(self, _conn, row, state, document, event_type, data):
        current = self.records[row["run_id"]]
        current.update(deepcopy(document))
        current.update(
            state=state,
            state_version=current["state_version"] + 1,
            last_event_seq=current["last_event_seq"] + 1,
        )
        self.records[row["run_id"]] = current
        self.event_log[row["run_id"]].append({
            "run_id": row["run_id"], "seq": current["last_event_seq"],
            "type": event_type, "state_version": current["state_version"],
            "occurred_at": "2026-01-01T00:00:00Z", "data": deepcopy(data),
        })
        return self.get(row["owner"], row["run_id"])


def _catalog(allowed_manifests=None):
    endpoints = [
        EndpointDescription(
            role=role, label="开发合成端点", base_url="https://example.invalid/v1",
            model="synthetic", version="v1",
        )
        for role in ("generator", "reviewer")
    ]
    return AuthorizationCatalog(
        endpoints, [], frozenset({canonical_digest([])})
        if allowed_manifests is None else frozenset(allowed_manifests),
    )


def _development_dependencies(roles=None, **updates):
    roles = roles or SyntheticRoles()
    catalog = updates.get("authorization_catalog", _MISSING)
    catalog_for_digests = _catalog() if catalog is _MISSING or catalog is None else catalog
    result = replace(
        dependencies(roles, "outbound"),
        endpoint_profile_digest=catalog_for_digests.endpoint_digest,
        knowledge_manifest_digest=catalog_for_digests.knowledge_digest,
        authorization_catalog=catalog_for_digests,
        runtime_roles_factory=lambda *_args: roles,
        force_engineering_exports=True,
        development_outbound_enabled=True,
    )
    return replace(result, **updates)


def _create(service):
    return service.create("owner", CreateRunRequest(
        request_id=uuid4(), session_id="development-outbound",
        accident_data={"事实": "仅用于隔离开发测试"}, evidence_revision=0,
    ))


def _authorize(service, run_id):
    preview = service.authorization_preview("owner", run_id)
    request = AuthorizationRequest(
        snapshot_digest=preview["snapshot_digest"],
        endpoint_profile_digest=preview["endpoint_profile_digest"],
        approved_knowledge_manifest_digest=preview["approved_knowledge_manifest_digest"],
        confirmed=True,
    )
    return service.authorize("owner", run_id, request)


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_development_outbound_enabled_requires_strict_bool(value):
    with pytest.raises(TypeError, match="严格布尔"):
        _development_dependencies(development_outbound_enabled=value)


@pytest.mark.parametrize("updates", [
    {"execution_profile": "synthetic_test"},
    {"force_engineering_exports": False},
    {"authorization_catalog": None},
    {"runtime_roles_factory": None},
    {"release_registry": object()},
    {"code_digest": "a" * 64},
])
def test_development_outbound_enabled_requires_isolated_dependencies(updates):
    with pytest.raises(ValueError):
        _development_dependencies(**updates)


def test_development_outbound_requires_persistent_authorize_before_runtime_roles():
    roles = SyntheticRoles()
    store = AuthorizableMemoryStore()
    service = ReportRunService(store, _development_dependencies(roles))
    run = _create(service)

    with pytest.raises(HarnessError, match="authorization_required"):
        asyncio.run(service.execute("owner", run["run_id"], 0))
    assert roles.calls == []

    approved = _authorize(service, run["run_id"])
    result = asyncio.run(service.execute("owner", run["run_id"], approved["state_version"]))

    assert result["state"] == "published"
    assert result["quality_gate"] == "engineering_only"
    assert not result["formal_export_eligible"]
    assert result["release_binding_status"] == "unapproved"
    assert roles.calls == ["prepare", "generate", "review"]


def test_development_outbound_rejects_approval_drift_and_formal_binding():
    for mutation in ("policy", "owner", "quality_gate", "release_binding"):
        roles = SyntheticRoles()
        store = AuthorizableMemoryStore()
        service = ReportRunService(store, _development_dependencies(roles))
        run = _create(service)
        approved = _authorize(service, run["run_id"])
        record = store.records[run["run_id"]]
        if mutation == "policy":
            record["approval"]["policy_digest"] = "0" * 64
        elif mutation == "owner":
            record["approval"]["owner_user_id"] = "other-owner"
        elif mutation == "quality_gate":
            record["quality_gate"] = "quality_validated"
        else:
            record["release_binding"] = {"code_digest": "a" * 64}

        with pytest.raises(HarnessError, match="authorization_stale"):
            service.preflight("owner", run["run_id"], approved["state_version"])
        assert roles.calls == []


def test_development_outbound_rejects_catalog_manifest_drift():
    roles = SyntheticRoles()
    catalog = _catalog()
    store = AuthorizableMemoryStore()
    service = ReportRunService(store, _development_dependencies(roles, authorization_catalog=catalog))
    run = _create(service)
    approved = _authorize(service, run["run_id"])
    catalog.allowed_manifests = frozenset()

    with pytest.raises(HarnessError, match="authorization_stale"):
        service.preflight("owner", run["run_id"], approved["state_version"])
    assert roles.calls == []
