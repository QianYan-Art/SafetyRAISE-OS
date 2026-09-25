"""公开接口的鉴权与路径边界回归：匿名不能消耗模型额度，客户端输入不能引出服务目录。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.api import deps
from app.api.error_handling import register_exception_handlers
from app.api.routes_input import _parse_upload_manifest, router as input_router
from app.core.exceptions import InputValidationError
from app.core.path_guard import is_safe_path_segment
from app.report_harness import legacy_ownership
from app.services.auth_service import AuthenticatedUser
from app.services.input_generation_service import InputGenerationService
from app.services.report_export_service import ReportExportService


def _user(role: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        id=f"{role}-1", username=f"{role}-1", display_name=None, role=role,
        is_active=True, created_at="", updated_at="",
    )


def _path_settings(root: Path) -> SimpleNamespace:
    root = root.resolve()

    def resolve_path(raw):
        candidate = Path(raw)
        return (candidate if candidate.is_absolute() else root / candidate).resolve()

    return SimpleNamespace(
        backend_data_dir_path=root,
        input_generation_workspace_dir_path=root,
        output_dir_path=root,
        resolve_path=resolve_path,
    )


class _UnusedInputService:
    """鉴权失败时不应触达服务。"""

    def __init__(self, settings):
        self.settings = settings
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        raise AssertionError("未通过鉴权的请求不能触发输入生成")


def _input_app(tmp_path: Path, user=None) -> tuple[FastAPI, _UnusedInputService]:
    service = _UnusedInputService(_path_settings(tmp_path))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(input_router)
    app.dependency_overrides[deps.get_input_generation_service] = lambda: service
    # 真实的 get_current_user 在没有令牌时直接拒绝；替换认证服务避免连接数据库。
    app.dependency_overrides[deps.get_auth_service] = lambda: object()
    if user is not None:
        app.dependency_overrides[deps.get_current_user] = lambda: user
    return app, service


@pytest.mark.parametrize("value", ["default_upload", "vehicle_exterior", "report-abc123", "trace-1690000000000"])
def test_safe_path_segment_accepts_generated_identifiers(value):
    assert is_safe_path_segment(value)


@pytest.mark.parametrize(
    "value",
    ["", ".", "..", "../x", "a/b", "a\\b", ".hidden", "-x", "x" * 129, "中文", "a b", None, 1],
)
def test_safe_path_segment_rejects_path_syntax(value):
    assert not is_safe_path_segment(value)


def test_upload_input_requires_login(tmp_path):
    app, service = _input_app(tmp_path)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/inputs/generate-from-upload",
            files={"files": ("scene.jpg", b"synthetic", "image/jpeg")},
            data={"upload_manifest": "{}"},
        )

    assert response.status_code == 401
    assert service.calls == 0


@pytest.mark.parametrize(("user", "status"), [(None, 401), (_user("user"), 403)])
def test_video_path_input_is_admin_only(tmp_path, user, status):
    app, service = _input_app(tmp_path, user=user)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/inputs/generate-from-video",
            json={"video_path": str(tmp_path / "other-user.mp4")},
        )

    assert response.status_code == status
    assert service.calls == 0


@pytest.mark.parametrize("category_id", ["../escape", "a/b", "..", ".hidden"])
def test_upload_manifest_rejects_path_like_category_ids(category_id):
    manifest = (
        '{"groups": [{"category_id": "%s", "category_label": "合成"}],'
        ' "items": [{"category_id": "%s", "media_type": "image", "original_name": "a.jpg"}]}'
    ) % (category_id, category_id)

    with pytest.raises(InputValidationError):
        _parse_upload_manifest(manifest, expected_count=1)


def test_media_preparation_rejects_category_outside_upload_dir(tmp_path):
    source = tmp_path / "source.jpg"
    source.write_bytes(b"synthetic")
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()
    service = SimpleNamespace(settings=_path_settings(tmp_path))
    entry = {"path": str(source), "category_id": "../outside", "media_type": "image", "sequence": 1}

    with pytest.raises(InputValidationError):
        InputGenerationService._prepare_media_inputs(service, [entry], uploads_dir)

    assert not (tmp_path / "outside").exists()


def test_output_dir_lookup_requires_single_trace_segment(tmp_path):
    for name in ("report-own", "report-other"):
        (tmp_path / name).mkdir()
    settings = _path_settings(tmp_path)

    assert legacy_ownership._resolve_output_dir(settings, "report-own", None) == (tmp_path / "report-own").resolve()
    assert legacy_ownership._resolve_output_dir(settings, "report-own/../report-other", None) is None

    exporter = ReportExportService.__new__(ReportExportService)
    exporter.settings = settings
    with pytest.raises(InputValidationError):
        exporter._resolve_output_dir("report-own/../report-other")


def test_ready_hides_failure_details_and_server_paths():
    class DegradedReadiness:
        def check(self):
            return {
                "status": "degraded",
                "ready": False,
                "checks": {
                    "upload_dir_writable": {
                        "ok": False,
                        "message": "上传目录不可写。",
                        "detail": "PermissionError: /srv/private/uploads",
                        "path": "/srv/private/uploads",
                    },
                },
            }

    response = main.ready(readiness_service=DegradedReadiness())
    body = json.loads(response.body)

    assert response.status_code == 503
    check = body["checks"]["upload_dir_writable"]
    assert check == {"ok": False, "message": "上传目录不可写。"}
    assert "/srv/private" not in response.body.decode("utf-8")
