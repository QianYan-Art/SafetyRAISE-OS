from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from app.report_harness.business_bootstrap import BusinessRuntimeManifest, assemble_business_runtime
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.execution import production_dependencies
from app.report_harness.release_registry import FileReleaseRegistry, verified_code_digest
from app.report_harness.resources import check_host_memory, check_storage

_MANIFEST_LIMIT = 1024 * 1024


def _read_manifest(path: Path) -> BusinessRuntimeManifest:
    raw = path.read_bytes()
    if len(raw) > _MANIFEST_LIMIT:
        raise HarnessError("production_manifest_unavailable", 503)
    return BusinessRuntimeManifest.model_validate_json(raw)


def assemble_production_runtime(settings, config):
    """只读装配主应用的业务运行时；不登记新预算、不签发批准、不迁移数据库。

    formal 模式要求批准表存在当前代码、策略、端点与知识的已批准绑定；
    demo 模式不读批准表，直接使用业务装配的工程语义（工程稿、强制带标记导出）。
    两种模式都在每次外发/工具步骤前复核资源与运行配置未被改动。
    """
    production_dependencies(config.model_dump(mode="json"))
    manifest_path = Path(config.runtime_manifest_path or "")
    if not manifest_path.is_absolute() or not manifest_path.is_file():
        raise HarnessError("production_manifest_unavailable", 503)
    manifest = _read_manifest(manifest_path)
    manifest_digest = canonical_digest(manifest)
    paths = tuple(Path(path) for path in config.resource_paths)
    if not paths or any(not path.is_absolute() or not path.is_dir() for path in paths):
        raise HarnessError("resource_probe_failed", 503)
    paths = (*paths, Path(manifest.ledger_path).resolve(strict=True).parent)

    def check_runtime():
        check_storage(paths, minimum_free_bytes=config.minimum_free_mib * 1024 * 1024)
        check_host_memory(minimum_available_bytes=config.minimum_memory_mib * 1024 * 1024)
        # 运行中改动 manifest 会改变预算与端点合同，已装配的运行时不得继续外发。
        current = manifest_path.read_bytes()
        if len(current) > _MANIFEST_LIMIT or canonical_digest(
            BusinessRuntimeManifest.model_validate_json(current)
        ) != manifest_digest:
            raise HarnessError("authorization_stale")

    if config.release_mode == "demo":
        check_runtime()
        return assemble_business_runtime(settings, manifest, resource_check=check_runtime)
    return _assemble_formal(settings, manifest, check_runtime)


def _assemble_formal(settings, manifest: BusinessRuntimeManifest, check_runtime):
    registry = FileReleaseRegistry()
    registry.validate()
    code_digest = verified_code_digest()
    if code_digest is None:
        raise HarnessError("release_code_unverified", 503)
    binding = None

    def check_runtime_and_release():
        check_runtime()
        # 每次外发/工具步骤复核，不让启动后的撤销或源码变化继续获得外发权限。
        if verified_code_digest() != code_digest:
            raise HarnessError("release_code_unverified", 503)
        if binding is not None and registry.status(binding) != "approved":
            raise HarnessError("release_not_approved", 503)

    check_runtime_and_release()
    runtime = assemble_business_runtime(
        settings, manifest, resource_check=check_runtime_and_release,
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
