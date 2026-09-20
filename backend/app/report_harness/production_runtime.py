from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.report_harness.business_bootstrap import BusinessRuntimeManifest, assemble_business_runtime
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.execution import production_dependencies
from app.report_harness.release_registry import FileReleaseRegistry, verified_code_digest
from app.report_harness.resources import check_host_memory, check_storage


def assemble_production_runtime(settings, config):
    """只读装配正式依赖；不登记新预算、不签发批准、不迁移数据库。"""
    production_dependencies(config.model_dump(mode="json"))
    manifest_path = Path(config.runtime_manifest_path or "")
    if not manifest_path.is_absolute() or not manifest_path.is_file():
        raise HarnessError("production_manifest_unavailable", 503)
    raw = manifest_path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise HarnessError("production_manifest_unavailable", 503)
    manifest = BusinessRuntimeManifest.model_validate_json(raw)
    manifest_digest = canonical_digest(manifest)
    paths = tuple(Path(path) for path in config.resource_paths)
    if not paths or any(not path.is_absolute() or not path.is_dir() for path in paths):
        raise HarnessError("resource_probe_failed", 503)
    paths = (*paths, Path(manifest.ledger_path).resolve(strict=True).parent)
    registry = FileReleaseRegistry()
    registry.validate()
    code_digest = verified_code_digest()
    if code_digest is None:
        raise HarnessError("release_code_unverified", 503)
    binding = None

    def check_resources_and_release():
        check_storage(paths, minimum_free_bytes=config.minimum_free_mib * 1024 * 1024)
        check_host_memory(minimum_available_bytes=config.minimum_memory_mib * 1024 * 1024)
        # 每次外发/工具步骤复核，不让启动后的撤销或源码变化继续获得外发权限。
        if verified_code_digest() != code_digest:
            raise HarnessError("release_code_unverified", 503)
        current = manifest_path.read_bytes()
        if len(current) > 1024 * 1024 or canonical_digest(
            BusinessRuntimeManifest.model_validate_json(current)
        ) != manifest_digest:
            raise HarnessError("authorization_stale")
        if binding is not None and registry.status(binding) != "approved":
            raise HarnessError("release_not_approved", 503)

    check_resources_and_release()
    runtime = assemble_business_runtime(
        settings, manifest, resource_check=check_resources_and_release,
    )
    binding = registry.binding_for({
        "code_digest": code_digest,
        "policy_digest": runtime.policy_digest,
        "model_endpoint_digest": runtime.endpoint_profile_digest,
        "knowledge_manifest_digest": runtime.knowledge_manifest_digest,
    })
    if binding is None or registry.status(binding) != "approved":
        raise HarnessError("release_not_approved", 503)
    return replace(
        runtime, development_outbound_enabled=False, production_outbound_enabled=True,
        force_engineering_exports=False, release_registry=registry, code_digest=code_digest,
    )
