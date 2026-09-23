from dataclasses import replace

import pytest

from app.report_harness import production_runtime as production
from app.report_harness.config import ReportHarnessSettings
from app.report_harness.errors import HarnessError
from tests.unit.test_business_bootstrap import configured


class Registry:
    def __init__(self):
        self.revoked = False
        self.binding = None

    def validate(self):
        return None

    def binding_for(self, digests):
        if self.revoked:
            return None
        self.binding = {**digests, "evaluation_evidence_digest": "a" * 64}
        return self.binding

    def status(self, binding):
        return "revoked" if self.revoked else "approved"


@pytest.fixture
def ready(configured, tmp_path, monkeypatch):
    settings, manifest, _ = configured
    path = tmp_path / "runtime.json"
    path.write_text(manifest.model_dump_json(), encoding="utf-8")
    config = ReportHarnessSettings(
        enabled=True, online_enabled=True, runtime_manifest_path=str(path),
        resource_paths=[str(tmp_path)],
    )
    registry = Registry()
    monkeypatch.setattr(production, "FileReleaseRegistry", lambda: registry)
    monkeypatch.setattr(production, "verified_code_digest", lambda: "c" * 64)
    monkeypatch.setattr(production, "check_storage", lambda *_a, **_k: None)
    monkeypatch.setattr(production, "check_host_memory", lambda **_k: None)
    return settings, config, registry


def test_production_requires_verified_release_and_preserves_business_factory(ready):
    settings, config, registry = ready
    runtime = production.assemble_production_runtime(settings, config)
    assert runtime.production_outbound_enabled
    assert not runtime.development_outbound_enabled
    assert not runtime.force_engineering_exports
    assert runtime.business_workflow and runtime.runtime_roles_factory
    assert runtime.release_registry is registry
    runtime.resource_check()
    with pytest.raises(ValueError):
        replace(runtime, development_outbound_enabled=True)
    with pytest.raises(ValueError):
        replace(runtime, code_digest=None)
    with pytest.raises(ValueError):
        replace(runtime, force_engineering_exports=True)


def test_revocation_after_startup_blocks_next_action(ready):
    settings, config, registry = ready
    runtime = production.assemble_production_runtime(settings, config)
    registry.revoked = True
    with pytest.raises(HarnessError, match="release_not_approved"):
        runtime.resource_check()


def test_missing_release_or_code_is_not_silently_downgraded(ready, monkeypatch):
    settings, config, registry = ready
    registry.revoked = True
    with pytest.raises(HarnessError, match="release_not_approved"):
        production.assemble_production_runtime(settings, config)
    registry.revoked = False
    monkeypatch.setattr(production, "verified_code_digest", lambda: None)
    with pytest.raises(HarnessError, match="release_code_unverified"):
        production.assemble_production_runtime(settings, config)


def test_code_change_after_startup_blocks_next_action(ready, monkeypatch):
    settings, config, _ = ready
    runtime = production.assemble_production_runtime(settings, config)
    monkeypatch.setattr(production, "verified_code_digest", lambda: "d" * 64)
    with pytest.raises(HarnessError, match="release_code_unverified"):
        runtime.resource_check()


def test_manifest_change_after_startup_blocks_next_action(ready):
    from pathlib import Path
    import json

    settings, config, _ = ready
    runtime = production.assemble_production_runtime(settings, config)
    path = Path(config.runtime_manifest_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["experiment_id"] = "changed"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(HarnessError, match="authorization_stale"):
        runtime.resource_check()


def test_production_rejects_relative_manifest_or_missing_resources(ready):
    settings, config, _ = ready
    with pytest.raises(HarnessError, match="production_manifest_unavailable"):
        production.assemble_production_runtime(
            settings, config.model_copy(update={"runtime_manifest_path": "runtime.json"}),
        )
    with pytest.raises(ValueError):
        production.assemble_production_runtime(
            settings, config.model_copy(update={"resource_paths": []}),
        )


def test_demo_mode_runs_engineering_only_without_reading_release_registry(ready, monkeypatch):
    from app.report_harness.execution import outbound_ready

    settings, config, _ = ready

    def registry_must_not_be_read():
        raise AssertionError("演示模式不得读取批准表")

    monkeypatch.setattr(production, "FileReleaseRegistry", registry_must_not_be_read)
    monkeypatch.setattr(production, "verified_code_digest", registry_must_not_be_read)
    runtime = production.assemble_production_runtime(
        settings, config.model_copy(update={"release_mode": "demo"}),
    )
    assert outbound_ready(runtime)
    assert not runtime.production_outbound_enabled
    assert runtime.development_outbound_enabled and runtime.force_engineering_exports
    assert runtime.release_registry is None and runtime.code_digest is None
    runtime.resource_check()


def test_demo_mode_still_blocks_after_manifest_change(ready):
    from pathlib import Path
    import json

    settings, config, _ = ready
    runtime = production.assemble_production_runtime(
        settings, config.model_copy(update={"release_mode": "demo"}),
    )
    path = Path(config.runtime_manifest_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["experiment_id"] = "changed"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(HarnessError, match="authorization_stale"):
        runtime.resource_check()


def test_unknown_release_mode_is_rejected():
    with pytest.raises(ValueError):
        ReportHarnessSettings(release_mode="approved-by-default")
