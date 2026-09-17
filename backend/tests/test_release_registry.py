import hashlib
import json
from dataclasses import replace

import pytest
from pydantic import ValidationError

from app.report_harness import release_registry as releases
from app.report_harness.config import ReportHarnessSettings
from app.report_harness.errors import HarnessError
from tests.test_run_exports import historical_report


def test_production_rejects_test_configuration_and_arbitrary_registry_paths():
    for config in (
        {"execution_profile": "synthetic_test"}, {"test_release_bindings": []},
        {"release_registry_path": "tests/bindings.json"},
        {"force_engineering_exports": True},
    ):
        with pytest.raises(ValidationError):
            ReportHarnessSettings.model_validate(config)


def test_current_writable_checkout_cannot_act_as_production_approval_store():
    with pytest.raises(HarnessError, match="release_registry_not_read_only"):
        releases.FileReleaseRegistry().validate()


def test_registry_reloads_revocation_and_verifies_local_evaluation_reference(tmp_path, monkeypatch):
    path = tmp_path / "approved_release_bindings.json"
    proof = b'{"result":"synthetic historical fixture, not a real approval"}'
    digest = hashlib.sha256(proof).hexdigest()
    evidence_dir = tmp_path / "approved_evaluations"
    evidence_dir.mkdir()
    (evidence_dir / f"{digest}.json").write_bytes(proof)
    record, _ = historical_report()
    binding = {**record["release_binding"], "evaluation_evidence_digest": digest}
    path.write_text(json.dumps([binding]), encoding="utf-8")
    monkeypatch.setattr(releases, "APPROVED_BINDINGS", path)
    # 测schema和重读逻辑，ACL负分支由真实只读检查测试覆盖。
    monkeypatch.setattr(releases, "_assert_no_write_access", lambda *_args, **_kwargs: None)
    registry = releases.FileReleaseRegistry()
    assert registry.status(binding) == "approved"
    assert registry.binding_for(binding) == binding
    revoked = {**binding, "revoked_at": binding["approved_at"], "revocation_reason": "测试撤销"}
    path.write_text(json.dumps([revoked]), encoding="utf-8")
    assert registry.status(binding) == "revoked"
    assert registry.binding_for(binding) is None
    (evidence_dir / f"{digest}.json").write_bytes(b"changed")
    with pytest.raises(HarnessError, match="release_registry_unavailable"):
        registry.validate()


def test_missing_build_manifest_cannot_grant_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, "APPROVED_BINDINGS", tmp_path / "approved_release_bindings.json")
    assert releases.verified_code_digest() is None


def test_synthetic_creation_never_asks_registry_for_quality(pg_store):
    from uuid import uuid4
    from app.schemas.report_run import CreateRunRequest
    from app.services.report_run_service import ReportRunService
    from tests.harness_fixtures import SyntheticRoles, dependencies

    class Registry:
        def binding_for(self, _digests):
            raise AssertionError("合成创建不得获得正式资格")

        def status(self, _binding):
            raise AssertionError("工程样本不应查询正式批准")

    store, owner, _, session = pg_store
    service = ReportRunService(store, replace(
        dependencies(SyntheticRoles()), release_registry=Registry(), code_digest="a" * 64,
    ))
    result = service.create(owner, CreateRunRequest(
        request_id=uuid4(), session_id=session, accident_data={"事实": "合成"},
        evidence_revision=0,
    ))
    assert result["quality_gate"] == "engineering_only"
    assert not result["formal_export_eligible"]
