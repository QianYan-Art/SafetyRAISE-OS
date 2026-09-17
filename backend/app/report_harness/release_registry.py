from __future__ import annotations

import json
import os
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import Field, TypeAdapter

from app.report_harness.errors import HarnessError
from app.schemas.base import StrictModel
from app.report_harness.contracts import canonical_digest

APPROVED_BINDINGS = (
    Path(__file__).resolve().parents[2] / "config/report_harness/approved_release_bindings.json"
)
DIGEST_FIELDS = (
    "code_digest", "policy_digest", "model_endpoint_digest", "knowledge_manifest_digest",
)


class ReleaseBinding(StrictModel):
    code_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_endpoint_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    knowledge_manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evaluation_evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: str = Field(min_length=1, max_length=200)
    approved_at: datetime
    revoked_at: datetime | None = None
    revocation_reason: str | None = Field(default=None, max_length=1000)


class ReleaseRegistry(Protocol):
    def status(self, binding: dict | None) -> str: ...
    def binding_for(self, digests: dict) -> dict | None: ...


def binding_status(entries: list[ReleaseBinding], binding: dict | None) -> str:
    if not isinstance(binding, dict):
        return "unapproved"
    matches = [entry for entry in entries if all(
        getattr(entry, key) == binding.get(key) for key in DIGEST_FIELDS
    )]
    if len(matches) != 1:
        return "missing"
    entry = matches[0]
    if entry.revoked_at is not None:
        return "revoked"
    if entry.evaluation_evidence_digest != binding.get("evaluation_evidence_digest"):
        return "missing"
    return "approved"


def assert_read_only(path: Path) -> None:
    """验证当前运行身份不能写批准表；失败关闭，不自动更改部署权限。"""
    if (path.is_symlink() or path.resolve() != path.absolute()
            or path.resolve() != APPROVED_BINDINGS.resolve()):
        raise HarnessError("release_registry_unavailable", 503)
    _assert_no_write_access(path, directory=False)
    _assert_no_write_access(path.parent, directory=True)


def _assert_no_write_access(path: Path, *, directory: bool) -> None:
    if os.name == "nt":
        # Windows 的 DOS 只读属性不等价于 ACL，不能用它冒充权限隔离。
        import ctypes
        from ctypes import wintypes

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        descriptor = ctypes.c_void_p()
        advapi.GetNamedSecurityInfoW.argtypes = [
            wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
        if advapi.GetNamedSecurityInfoW(
            str(path), 1, 7, None, None, None, None, ctypes.byref(descriptor)
        ) != 0:
            raise HarnessError("release_registry_unavailable", 503)
        token, impersonation = wintypes.HANDLE(), wintypes.HANDLE()
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.LocalFree.argtypes = [ctypes.c_void_p]

        class GenericMapping(ctypes.Structure):
            _fields_ = [(name, wintypes.DWORD) for name in (
                "read", "write", "execute", "all",
            )]

        mapping = GenericMapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
        privilege = ctypes.create_string_buffer(4096)
        size = wintypes.DWORD(len(privilege))
        granted, allowed = wintypes.DWORD(), wintypes.BOOL()
        advapi.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi.DuplicateToken.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi.AccessCheck.argtypes = [
            ctypes.c_void_p, wintypes.HANDLE, wintypes.DWORD,
            ctypes.POINTER(GenericMapping), ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.BOOL),
        ]
        try:
            if (not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0xA, ctypes.byref(token))
                    or not advapi.DuplicateToken(token, 2, ctypes.byref(impersonation))
                    or not advapi.AccessCheck(
                        descriptor, impersonation, 0x02000000, ctypes.byref(mapping), privilege,
                        ctypes.byref(size), ctypes.byref(granted), ctypes.byref(allowed),
                    ) or not allowed.value
                    or granted.value & (0xD0046 if directory else 0xD0006)):
                raise HarnessError("release_registry_not_read_only", 503)
        finally:
            if impersonation:
                kernel.CloseHandle(impersonation)
            if token:
                kernel.CloseHandle(token)
            kernel.LocalFree(descriptor)
    elif os.access(path, os.W_OK):
        raise HarnessError("release_registry_not_read_only", 503)


class FileReleaseRegistry:
    """唯一生产路径，每次查询重新读取，撤销立即生效。"""

    def _entries(self) -> list[ReleaseBinding]:
        try:
            assert_read_only(APPROVED_BINDINGS)
            if APPROVED_BINDINGS.stat().st_size > 1024 * 1024:
                raise ValueError("批准表过大")
            entries = TypeAdapter(list[ReleaseBinding]).validate_python(
                json.loads(APPROVED_BINDINGS.read_text(encoding="utf-8"))
            )
            identities = [tuple(getattr(entry, key) for key in DIGEST_FIELDS) for entry in entries]
            if len(set(identities)) != len(identities):
                raise ValueError("批准绑定重复")
            for entry in entries:
                evidence = APPROVED_BINDINGS.parent / "approved_evaluations" / (
                    entry.evaluation_evidence_digest + ".json"
                )
                if (evidence.is_symlink() or evidence.resolve() != evidence.absolute()
                        or evidence.stat().st_size > 1024 * 1024):
                    raise ValueError("验收证据引用无效")
                _assert_no_write_access(evidence, directory=False)
                _assert_no_write_access(evidence.parent, directory=True)
                proof = evidence.read_bytes()
                if hashlib.sha256(proof).hexdigest() != entry.evaluation_evidence_digest:
                    raise ValueError("验收证据摘要不符")
                if not isinstance(json.loads(proof), dict):
                    raise ValueError("验收证据须为JSON对象")
                if entry.approved_at.tzinfo is None or (
                    entry.revoked_at is not None and (
                        entry.revoked_at.tzinfo is None or not entry.revocation_reason
                    )
                ):
                    raise ValueError("批准时间或撤销记录不完整")
            return entries
        except HarnessError:
            raise
        except (OSError, ValueError) as exc:
            raise HarnessError("release_registry_unavailable", 503) from exc

    def status(self, binding: dict | None) -> str:
        return binding_status(self._entries(), binding)

    def binding_for(self, digests: dict) -> dict | None:
        for entry in self._entries():
            if entry.revoked_at is None and all(
                getattr(entry, key) == digests.get(key) for key in DIGEST_FIELDS
            ):
                return entry.model_dump(mode="json")
        return None

    def validate(self) -> None:
        self._entries()


def verified_code_digest() -> str | None:
    """缺构建清单或源码不一致时不赋予质量资格；不在服务端执行构建/修改清单。"""
    root = Path(__file__).resolve().parents[3]
    manifest_path = APPROVED_BINDINGS.parent / "build_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (type(manifest.get("version")) is not int or manifest["version"] != 1
                or manifest.get("clean") is not True):
            return None
        paths = sorted(
            list((root / "backend/app").rglob("*.py"))
            + list((root / "frontend/src").rglob("*.ts"))
            + list((root / "frontend/src").rglob("*.tsx"))
            + list((root / "frontend/src").rglob("*.css"))
            + list(APPROVED_BINDINGS.parent.glob("*.md"))
            + [root / name for name in (
                "backend/requirements.txt", "backend/requirements-video.txt",
                "frontend/package.json", "frontend/package-lock.json",
            )]
        )
        if any(path.is_symlink() or not path.resolve().is_relative_to(root) for path in paths):
            return None
        source_hashes = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        }
        digest = canonical_digest(source_hashes)
        if manifest.get("source_hashes") != source_hashes or manifest.get("code_digest") != digest:
            return None
        return digest
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def export_eligibility(record: dict, registry: ReleaseRegistry | None) -> tuple[bool, str]:
    if record.get("quality_gate") != "quality_validated":
        return False, "unapproved"
    if record.get("execution_profile") == "synthetic_test":
        return False, "unapproved"
    status = registry.status(record.get("release_binding")) if registry else "unavailable"
    return record.get("state") == "published" and status == "approved", status
