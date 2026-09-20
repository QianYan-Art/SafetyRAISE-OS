from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from psycopg import OperationalError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import (
    get_current_user,
    get_optional_current_user,
    get_report_export_service,
    get_report_service,
    get_user_capability_config_service,
)
from app.api.error_handling import register_exception_handlers
import app.api.routes_report as report_routes
from app.api.routes_report import router
from app.core.exceptions import InputValidationError, SessionNotFoundError
from app.report_harness import legacy_api_adapter as adapter
from app.report_harness.authorization import AuthorizationCatalog, EndpointDescription
from app.report_harness.config import ReportHarnessSettings
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.legacy_ownership import LEGACY_OWNERSHIP_FILENAME
from app.report_harness.store import RunStore
from app.schemas.report import ReportResult
from app.schemas.input_generation import InputGenerationArtifact
from app.services.report_run_service import ReportRunService
from app.services.auth_service import AuthenticatedUser
from tests.harness_fixtures import SyntheticRoles, dependencies


class OldServiceBomb:
    def __init__(self):
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        raise AssertionError("在线旧 API 不得回退 ReportService.generate")


class FakeStore:
    def __init__(self):
        self.events_by_run: dict[str, list[dict]] = {}

    def events(self, owner, run_id, after_seq=0):
        events = self.events_by_run.get(run_id, [])
        selected = [event for event in events if event["seq"] > after_seq]
        return {"events": selected, "next_seq": selected[-1]["seq"] if selected else after_seq}


class FakeHarnessService:
    def __init__(self, *, formal=True, execute_delay=0):
        self.store = FakeStore()
        self.formal = formal
        self.execute_delay = execute_delay
        self.calls: list[tuple] = []
        self.run_id = str(uuid4())

    def create(self, owner, payload):
        self.calls.append(("create", owner, payload))
        return {"run_id": self.run_id, "state": "queued", "state_version": 0}

    def authorization_preview(self, owner, run_id):
        self.calls.append(("preview", owner, run_id))
        return {
            "available": True,
            "snapshot_digest": "a" * 64,
            "endpoint_profile_digest": "b" * 64,
            "approved_knowledge_manifest_digest": "c" * 64,
        }

    def authorize(self, owner, run_id, payload):
        self.calls.append(("authorize", owner, run_id, payload))
        return {"run_id": run_id, "state": "queued", "state_version": 1}

    async def execute(self, owner, run_id, expected_version):
        self.calls.append(("execute", owner, run_id, expected_version))
        if self.execute_delay:
            await asyncio.sleep(self.execute_delay)
        self.store.events_by_run[run_id] = [{
            "seq": 1, "type": "stage", "data": {"stage": "preparing"},
        }, {
            "seq": 2, "type": "final", "data": {"state": "published"},
        }]
        return {
            "run_id": run_id,
            "state": "published",
            "quality_gate": "quality_validated" if self.formal else "engineering_only",
            "formal_export_eligible": self.formal,
            "report": {"report_markdown": "正式 harness 报告"},
        }

    def cancel(self, owner, run_id):
        self.calls.append(("cancel", owner, run_id))
        return {"run_id": run_id, "state": "cancelled"}


class FakeEvidenceStore:
    def __init__(self, store):
        self.store = store

    def get(self, owner, session_id):
        assert owner == "owner"
        return {"revision": 4}


class FakeLegacyChatSessionService:
    def __init__(self):
        self.records = {
            "session-1": SimpleNamespace(id="session-1", draft_meta={}),
        }
        self.calls: list[tuple] = []
        self.created: list = []
        self.updated: list = []

    def get_session(self, session_id, **kwargs):
        self.calls.append(("get", session_id))
        record = self.records.get(session_id)
        if record is None:
            raise SessionNotFoundError(f"合成测试会话不存在: {session_id}")
        return record

    def create_session(self, request):
        self.calls.append(("create", request.id))
        record = SimpleNamespace(
            id=request.id,
            draft_meta=request.draft_meta or {},
            draft_json=request.draft_json,
        )
        self.records[record.id] = record
        self.created.append(request)
        return record

    def update_session(self, session_id, request):
        self.calls.append(("update", session_id))
        record = self.records[session_id]
        if request.draft_meta is not None:
            record.draft_meta = request.draft_meta
        if request.draft_json is not None:
            record.draft_json = request.draft_json
        self.updated.append((session_id, request))
        return record


class FakeInputGenerationService:
    def __init__(self, settings, artifact=None, error=None):
        self.settings = settings
        self.artifact = artifact
        self.error = error
        self.calls: list[tuple] = []
        self.closed = False

    def generate(self, *, video_path, persist_generated_input):
        self.calls.append((video_path, persist_generated_input))
        if self.error is not None:
            raise self.error
        return self.artifact

    def close(self):
        self.closed = True


def _user() -> AuthenticatedUser:
    return AuthenticatedUser(
        id="owner", username="owner", display_name=None, role="user",
        is_active=True, created_at="", updated_at="",
    )


def _app(
    service: FakeHarnessService,
    old_service: OldServiceBomb,
    *,
    session_service: FakeLegacyChatSessionService | None = None,
    input_generation_service: FakeInputGenerationService | None = None,
) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_report_service] = lambda: old_service
    app.dependency_overrides[get_optional_current_user] = _user
    app.dependency_overrides[get_user_capability_config_service] = lambda: object()
    app.dependency_overrides[adapter.get_legacy_harness_context] = lambda: adapter.LegacyHarnessContext(
        store=service.store,
        service=service,
        chat_session_service=session_service or FakeLegacyChatSessionService(),
    )
    if input_generation_service is not None:
        app.dependency_overrides[deps.get_input_generation_service] = lambda: input_generation_service
    return app


def _payload(**overrides):
    payload = {
        "session_id": "session-1",
        "accident_data": {"事实": "在线旧接口合成测试"},
    }
    payload.update(overrides)
    return payload


def test_online_legacy_sync_preserves_success_status_without_fabricating_output_dir(monkeypatch):
    service = FakeHarnessService()
    old_service = OldServiceBomb()
    monkeypatch.setattr(adapter, "EvidenceStore", FakeEvidenceStore)
    app = _app(service, old_service)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/generate",
            json=_payload(),
            headers={"Idempotency-Key": "legacy-request-1"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["guidance"] == {}
    assert body["output_dir"] == ""
    assert body["report"]["report_markdown"] == "正式 harness 报告"
    assert old_service.calls == 0
    assert [call[0] for call in service.calls] == ["create", "preview", "authorize", "execute"]
    assert service.calls[0][2].evidence_revision == 4
    assert isinstance(service.calls[0][2].request_id, UUID)


@pytest.mark.parametrize("endpoint", ["generate", "generate/stream"])
def test_online_database_factory_failure_returns_503_without_old_service(monkeypatch, endpoint):
    service, old_service = FakeHarnessService(), OldServiceBomb()
    app = _app(service, old_service)
    del app.dependency_overrides[adapter.get_legacy_harness_context]
    app.state.report_harness_runtime = SimpleNamespace(production_outbound_enabled=True)
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(
        report_harness=ReportHarnessSettings(enabled=True, online_enabled=True),
    ))

    def failed_database():
        raise OperationalError("合成数据库连接故障")

    app.dependency_overrides[deps.get_database_service] = failed_database
    with TestClient(app) as client:
        response = client.post(f"/api/v1/reports/{endpoint}", json=_payload())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "legacy_database_unavailable"
    assert old_service.calls == 0
    assert service.calls == []


@pytest.mark.parametrize("endpoint", ["generate", "generate/stream"])
@pytest.mark.parametrize("phase", ["get", "create", "update"])
def test_online_session_database_failure_prevents_report_run(monkeypatch, endpoint, phase):
    service, old_service = FakeHarnessService(), OldServiceBomb()
    session_service = FakeLegacyChatSessionService()
    method = {"get": "get_session", "create": "create_session", "update": "update_session"}[phase]

    def failed_operation(*args, **kwargs):
        raise OperationalError("合成会话数据库故障")

    monkeypatch.setattr(session_service, method, failed_operation)
    app = _app(service, old_service, session_service=session_service)
    with TestClient(app) as client:
        response = client.post(
            f"/api/v1/reports/{endpoint}",
            json=_payload(session_id="session-1" if phase == "get" else None),
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "legacy_database_unavailable"
    assert old_service.calls == 0
    assert service.calls == []


def _path_settings(root: Path):
    root = root.resolve()

    def resolve_path(raw):
        candidate = Path(raw)
        return (candidate if candidate.is_absolute() else root / candidate).resolve()

    return SimpleNamespace(
        report_harness=ReportHarnessSettings(enabled=True, online_enabled=True),
        backend_data_dir_path=root,
        input_generation_workspace_dir_path=root,
        output_dir_path=root,
        resolve_path=resolve_path,
    )


def test_online_legacy_without_session_creates_owner_bound_session(monkeypatch):
    service = FakeHarnessService()
    old_service = OldServiceBomb()
    session_service = FakeLegacyChatSessionService()

    monkeypatch.setattr(adapter, "EvidenceStore", FakeEvidenceStore)
    app = _app(service, old_service, session_service=session_service)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/generate",
            json={"accident_data": {"事实": "没有会话但需要保留旧契约"}},
            headers={"Idempotency-Key": "legacy-session-create-1"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["retrieval_meta"]["session_created"] is True
    assert body["retrieval_meta"]["session_id"] == session_service.created[0].id
    assert session_service.created[0].draft_meta["legacy_report_api"] is True
    assert session_service.updated[0][1].draft_json == json.dumps(
        {"事实": "没有会话但需要保留旧契约"}, ensure_ascii=False,
    )
    assert service.calls[0][2].session_id == session_service.created[0].id
    assert old_service.calls == 0


def test_online_legacy_input_path_is_frozen_into_harness_snapshot(monkeypatch, tmp_path):
    service = FakeHarnessService()
    old_service = OldServiceBomb()
    input_path = tmp_path / "legacy-input.json"
    input_path.write_text('{"事实": "来自原 input_path 的合成快照"}', encoding="utf-8")
    monkeypatch.setattr(adapter, "EvidenceStore", FakeEvidenceStore)
    monkeypatch.setattr(deps, "get_settings", lambda: _path_settings(tmp_path))
    app = _app(service, old_service)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/generate",
            json={"session_id": "session-1", "input_path": str(input_path)},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["retrieval_meta"]["legacy_source_type"] == "input_path"
    assert body["retrieval_meta"]["legacy_source_path"] == str(input_path.resolve())
    assert service.calls[0][2].accident_data == {"事实": "来自原 input_path 的合成快照"}
    assert old_service.calls == 0


def test_online_legacy_video_path_reuses_original_input_generation_and_enters_harness(
    monkeypatch,
    tmp_path,
):
    service = FakeHarnessService()
    old_service = OldServiceBomb()
    video_path = tmp_path / "legacy-video.mp4"
    video_path.write_bytes(b"synthetic video placeholder")
    artifact = InputGenerationArtifact(
        media_type="video",
        input_path=str(tmp_path / "generated-input.json"),
        generated_input={"事实": "视觉前置产生的合成快照"},
        workspace_dir=str(tmp_path / "workspace"),
        raw_response_path=str(tmp_path / "raw-response.json"),
    )
    input_service = FakeInputGenerationService(_path_settings(tmp_path), artifact=artifact)
    session_service = FakeLegacyChatSessionService()
    monkeypatch.setattr(adapter, "EvidenceStore", FakeEvidenceStore)
    monkeypatch.setattr(deps, "get_settings", lambda: _path_settings(tmp_path))
    app = _app(
        service,
        old_service,
        session_service=session_service,
        input_generation_service=input_service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/generate",
            json={"session_id": "session-1", "video_path": str(video_path)},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["input_generation"]["generated_input"] == {"事实": "视觉前置产生的合成快照"}
    assert body["input_generation"]["input_path"] == str(tmp_path / "generated-input.json")
    assert body["output_dir"] == ""
    assert body["retrieval_meta"]["legacy_source_type"] == "video_path"
    assert service.calls[0][2].accident_data == {"事实": "视觉前置产生的合成快照"}
    assert input_service.calls == [(str(video_path.resolve()), True)]
    assert input_service.closed is True
    assert old_service.calls == 0


def test_online_legacy_video_owner_is_checked_before_path_and_visual_access(monkeypatch, tmp_path):
    service = FakeHarnessService()
    old_service = OldServiceBomb()
    video_path = tmp_path / "legacy-video.mp4"
    video_path.write_bytes(b"synthetic video placeholder")
    input_service = FakeInputGenerationService(
        _path_settings(tmp_path),
        artifact=InputGenerationArtifact(
            input_path=str(tmp_path / "generated-input.json"),
            generated_input={"事实": "不应执行"},
            workspace_dir=str(tmp_path / "workspace"),
        ),
    )
    session_service = FakeLegacyChatSessionService()
    monkeypatch.setattr(adapter, "EvidenceStore", FakeEvidenceStore)
    monkeypatch.setattr(deps, "get_settings", lambda: _path_settings(tmp_path))
    app = _app(
        service,
        old_service,
        session_service=session_service,
        input_generation_service=input_service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/generate",
            json={"session_id": "other-user-session", "video_path": str(video_path)},
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert input_service.calls == []
    assert service.calls == []
    assert old_service.calls == 0


def test_online_legacy_video_failure_creates_no_report_run_and_never_falls_back(
    monkeypatch,
    tmp_path,
):
    service = FakeHarnessService()
    old_service = OldServiceBomb()
    video_path = tmp_path / "legacy-video.mp4"
    video_path.write_bytes(b"synthetic video placeholder")
    input_service = FakeInputGenerationService(
        _path_settings(tmp_path),
        error=InputValidationError("合成视觉前置失败"),
    )
    monkeypatch.setattr(adapter, "EvidenceStore", FakeEvidenceStore)
    monkeypatch.setattr(deps, "get_settings", lambda: _path_settings(tmp_path))
    app = _app(
        service,
        old_service,
        input_generation_service=input_service,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/generate",
            json={"session_id": "session-1", "video_path": str(video_path)},
        )

    assert response.status_code == 400
    assert service.calls == []
    assert old_service.calls == 0
    assert input_service.closed is True


@pytest.mark.parametrize("field", ["input_path", "video_path"])
def test_online_legacy_input_paths_keep_root_guard(monkeypatch, tmp_path, field):
    service = FakeHarnessService()
    old_service = OldServiceBomb()
    outside = (tmp_path.parent / f"outside-{field}.json").resolve()
    monkeypatch.setattr(adapter, "EvidenceStore", FakeEvidenceStore)
    monkeypatch.setattr(deps, "get_settings", lambda: _path_settings(tmp_path))
    app = _app(service, old_service)

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/generate",
            json={"session_id": "session-1", field: str(outside)},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert old_service.calls == 0
    assert service.calls == []


def test_legacy_harness_export_is_owner_scoped(monkeypatch):
    run_id = str(uuid4())
    record = {
        "run_id": run_id,
        "state": "published",
        "quality_gate": "quality_validated",
        "execution_profile": "outbound",
        "release_binding": {"synthetic": "binding"},
        "report": {"report_markdown": "# 仅 owner 可下载"},
    }

    class Registry:
        def status(self, binding):
            return "approved"

    class OwnedStore:
        def get(self, owner, requested_run_id):
            if owner != "owner":
                raise HarnessError("not_found", 404)
            assert requested_run_id == run_id
            return record

    context = adapter.LegacyHarnessContext(
        store=OwnedStore(),
        service=SimpleNamespace(
            dependencies=SimpleNamespace(release_registry=Registry()),
        ),
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    principal = {"value": _user()}
    app.dependency_overrides[get_current_user] = lambda: principal["value"]
    app.dependency_overrides[adapter.get_legacy_harness_context] = lambda: context
    app.dependency_overrides[get_report_export_service] = lambda: SimpleNamespace(
        get_media_type=lambda export_format: "text/markdown",
    )
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(
        report_harness=ReportHarnessSettings(enabled=True, online_enabled=True),
    ))

    with TestClient(app) as client:
        owner_response = client.get(f"/api/v1/reports/report-{run_id}/exports/md")
        principal["value"] = AuthenticatedUser(
            id="other", username="other", display_name=None, role="user",
            is_active=True, created_at="", updated_at="",
        )
        other_response = client.get(f"/api/v1/reports/report-{run_id}/exports/md")

    assert owner_response.status_code == 200
    assert owner_response.text == "# 仅 owner 可下载"
    assert other_response.status_code == 404
    assert other_response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("export_format", ["pdf", "docx"])
def test_legacy_formal_export_rechecks_release_after_binary_render(
    monkeypatch,
    export_format,
):
    run_id = str(uuid4())
    record = {
        "run_id": run_id,
        "state": "published",
        "quality_gate": "quality_validated",
        "execution_profile": "outbound",
        "release_binding": {"synthetic": "binding"},
        "report": {"report_markdown": "# 渲染期间撤销"},
    }

    class RevokingRegistry:
        def __init__(self):
            self.calls = 0

        def status(self, binding):
            self.calls += 1
            return "approved" if self.calls == 1 else "revoked"

    class Store:
        def get(self, owner, requested_run_id):
            assert owner == "owner"
            assert requested_run_id == run_id
            return record

    class Renderer:
        def __init__(self):
            self.rendered = []

        def get_media_type(self, format_name):
            return "application/octet-stream"

        def _parse_markdown_blocks(self, markdown):
            return []

        def _build_docx(self, path, blocks, markdown, trace_id, verification_marker=None):
            self.rendered.append("docx")
            path.write_bytes(b"synthetic-docx")

        def _resolve_pdf_cover_config(self, blocks, trace_id, cover_options):
            return None

        def _build_pdf(self, path, blocks, trace_id, cover, verification_marker=None):
            self.rendered.append("pdf")
            path.write_bytes(b"synthetic-pdf")

    registry = RevokingRegistry()
    renderer = Renderer()
    context = adapter.LegacyHarnessContext(
        store=Store(),
        service=SimpleNamespace(
            dependencies=SimpleNamespace(release_registry=registry),
        ),
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[adapter.get_legacy_harness_context] = lambda: context
    app.dependency_overrides[get_report_export_service] = lambda: renderer

    with TestClient(app) as client:
        response = client.get(f"/api/v1/reports/report-{run_id}/exports/{export_format}")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "release_binding_revoked"
    assert renderer.rendered == [export_format]
    assert registry.calls == 2


def test_legacy_export_requires_persistent_owner_binding_for_old_service(tmp_path, monkeypatch):
    output_dir = tmp_path / "legacy-trace"
    output_dir.mkdir()
    report_path = output_dir / "report.md"
    report_path.write_text("# 旧服务 owner 绑定", encoding="utf-8")
    (output_dir / LEGACY_OWNERSHIP_FILENAME).write_text(
        json.dumps({
            "version": 1,
            "trace_id": "legacy-trace",
            "owner_user_id": "owner",
            "owner_username": "owner",
            "session_id": "legacy-session",
            "output_dir": str(output_dir.resolve()),
        }),
        encoding="utf-8",
    )

    class ExportService:
        def get_export_path(self, trace_id, export_format, pdf_cover_options=None):
            return report_path

        def get_media_type(self, export_format):
            return "text/markdown"

        def build_download_name(self, trace_id, export_format):
            return "legacy.md"

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    principal = {"value": _user()}
    app.dependency_overrides[get_current_user] = lambda: principal["value"]
    app.dependency_overrides[adapter.get_legacy_harness_context] = lambda: None
    app.dependency_overrides[get_report_export_service] = ExportService
    app.dependency_overrides[deps.get_database_service] = lambda: (_ for _ in ()).throw(
        RuntimeError("合成测试不连接业务数据库")
    )
    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: SimpleNamespace(output_dir_path=tmp_path),
    )

    with TestClient(app) as client:
        owner_response = client.get("/api/v1/reports/legacy-trace/exports/md")
        unbound_response = client.get("/api/v1/reports/unbound-trace/exports/md")
        principal["value"] = AuthenticatedUser(
            id="other", username="other", display_name=None, role="user",
            is_active=True, created_at="", updated_at="",
        )
        other_response = client.get("/api/v1/reports/legacy-trace/exports/md")

    assert owner_response.status_code == 200
    assert owner_response.text == "# 旧服务 owner 绑定"
    assert unbound_response.status_code == 404
    assert unbound_response.json()["error"]["code"] == "legacy_export_owner_required"
    assert other_response.status_code == 404
    assert other_response.json()["error"]["code"] == "legacy_export_owner_required"


def test_legacy_old_generation_rejects_database_persistence_failure_without_sidecar(
    tmp_path,
    monkeypatch,
):
    trace_id = "legacy-restart-trace"
    output_dir = tmp_path / trace_id
    output_dir.mkdir()
    (output_dir / "report.md").write_text("# 重启后仍可下载", encoding="utf-8")

    class OldService:
        def __init__(self):
            self.settings = SimpleNamespace(output_dir_path=tmp_path)

        def generate(self, **kwargs):
            return SimpleNamespace(
                trace_id=trace_id,
                output_dir=str(output_dir),
                guidance={"source": "synthetic"},
                report=ReportResult(report_markdown="重启后仍可下载"),
                input_generation=None,
                initial_knowledge_snippets=[],
                knowledge_snippets=[],
                retrieval_meta={},
                agentic_retrieval_rounds=[],
            )

    def build_app(service, principal):
        app = FastAPI()
        register_exception_handlers(app)
        app.include_router(router)
        app.dependency_overrides[get_report_service] = lambda: service
        app.dependency_overrides[get_optional_current_user] = lambda: principal["value"]
        app.dependency_overrides[get_current_user] = lambda: principal["value"]
        app.dependency_overrides[get_user_capability_config_service] = lambda: SimpleNamespace(
            resolve_overrides=lambda current_user: None,
        )
        app.dependency_overrides[adapter.get_legacy_harness_context] = lambda: None
        app.dependency_overrides[deps.get_database_service] = lambda: (_ for _ in ()).throw(
            RuntimeError("合成测试不连接业务数据库")
        )
        app.dependency_overrides[get_report_export_service] = lambda: SimpleNamespace(
            settings=SimpleNamespace(output_dir_path=tmp_path),
            get_export_path=lambda trace_id, export_format, pdf_cover_options=None: output_dir / "report.md",
            get_media_type=lambda export_format: "text/markdown",
            build_download_name=lambda trace_id, export_format: "legacy-restart.md",
        )
        return app

    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: SimpleNamespace(
            output_dir_path=tmp_path,
            report_harness=ReportHarnessSettings(enabled=True, online_enabled=False),
        ),
    )
    principal = {"value": _user()}
    with TestClient(build_app(OldService(), principal)) as client:
        generated = client.post(
            "/api/v1/reports/generate",
            json={
                "accident_data": {"事实": "持久归属回归"},
            },
        )
    assert generated.status_code == 503
    assert generated.json()["error"]["code"] == "legacy_ownership_unavailable"
    assert not (output_dir / LEGACY_OWNERSHIP_FILENAME).exists()


def test_legacy_old_generation_with_explicit_no_database_keeps_sidecar_compatibility(
    tmp_path,
    monkeypatch,
):
    trace_id = "legacy-no-database-compat-trace"
    output_dir = tmp_path / trace_id
    output_dir.mkdir()

    class OldService:
        settings = SimpleNamespace(output_dir_path=tmp_path)

        def generate(self, **kwargs):
            return SimpleNamespace(
                trace_id=trace_id,
                output_dir=str(output_dir),
                guidance={"source": "synthetic"},
                report=ReportResult(report_markdown="显式无数据库兼容"),
                input_generation=None,
                initial_knowledge_snippets=[],
                knowledge_snippets=[],
                retrieval_meta={},
                agentic_retrieval_rounds=[],
            )

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_report_service] = OldService
    app.dependency_overrides[get_optional_current_user] = _user
    app.dependency_overrides[get_user_capability_config_service] = lambda: SimpleNamespace(
        resolve_overrides=lambda current_user: None,
    )
    app.dependency_overrides[adapter.get_legacy_harness_context] = lambda: None
    app.dependency_overrides[deps.get_database_service] = lambda: None
    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: SimpleNamespace(
            output_dir_path=tmp_path,
            report_harness=ReportHarnessSettings(enabled=True, online_enabled=False),
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/generate",
            json={"accident_data": {"事实": "显式无数据库兼容"}},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "success"
    assert (output_dir / LEGACY_OWNERSHIP_FILENAME).exists()
    ownership = json.loads(
        (output_dir / LEGACY_OWNERSHIP_FILENAME).read_text(encoding="utf-8")
    )
    assert ownership["owner_user_id"] == "owner"


def test_legacy_old_generation_without_session_persists_bound_session(tmp_path, monkeypatch):
    trace_id = "legacy-no-session-trace"
    output_dir = tmp_path / trace_id
    output_dir.mkdir()

    class Result:
        def fetchone(self):
            return None

        def fetchall(self):
            return []

    class Connection:
        def __init__(self):
            self.calls = []

        def execute(self, statement, params=()):
            self.calls.append((statement, params))
            return Result()

        @contextmanager
        def transaction(self):
            yield self

    connection = Connection()

    class Database:
        @contextmanager
        def connection(self):
            yield connection

    class OldService:
        settings = SimpleNamespace(output_dir_path=tmp_path)

        def generate(self, **kwargs):
            return SimpleNamespace(
                trace_id=trace_id,
                output_dir=str(output_dir),
                guidance={},
                report=ReportResult(report_markdown="无会话旧直调"),
                input_generation=None,
                initial_knowledge_snippets=[],
                knowledge_snippets=[],
                retrieval_meta={},
                agentic_retrieval_rounds=[],
            )

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_report_service] = OldService
    app.dependency_overrides[get_optional_current_user] = _user
    app.dependency_overrides[get_user_capability_config_service] = lambda: SimpleNamespace(
        resolve_overrides=lambda current_user: None,
    )
    app.dependency_overrides[adapter.get_legacy_harness_context] = lambda: None
    app.dependency_overrides[deps.get_database_service] = lambda: Database()
    monkeypatch.setattr(
        deps,
        "get_settings",
        lambda: SimpleNamespace(
            output_dir_path=tmp_path,
            report_harness=ReportHarnessSettings(enabled=True, online_enabled=False),
        ),
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/generate",
            json={"accident_data": {"事实": "无会话仍需绑定"}},
        )

    assert response.status_code == 200
    bound_session_id = response.json()["retrieval_meta"]["session_id"]
    assert bound_session_id.startswith("legacy-report-")
    ownership = json.loads(
        (output_dir / LEGACY_OWNERSHIP_FILENAME).read_text(encoding="utf-8")
    )
    assert ownership["session_id"] == bound_session_id
    assert any("insert into chat_sessions" in statement.lower() for statement, _ in connection.calls)


def test_legacy_export_rehydrates_chat_session_owner_after_app_reset(
    pg_store, tmp_path, monkeypatch,
):
    store, owner, other, session_id = pg_store
    trace_id = "legacy-pg-restart-trace"
    output_dir = tmp_path / trace_id
    output_dir.mkdir()
    (output_dir / "report.md").write_text("# PostgreSQL 持久归属", encoding="utf-8")
    (output_dir / "run_log.json").write_text(
        json.dumps({"trace_id": trace_id, "session_id": session_id}),
        encoding="utf-8",
    )
    with store.connection() as conn, conn.transaction():
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS report_result jsonb")
        conn.execute("ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS updated_at bigint DEFAULT 0")
        conn.execute(
            """
            update chat_sessions
            set report_result=%s::jsonb, updated_at=%s
            where id=%s
            """,
            (
                json.dumps({
                    "trace_id": trace_id,
                    "output_dir": str(output_dir.resolve()),
                    "report": {"report_markdown": "PostgreSQL 持久归属"},
                }),
                1,
                session_id,
            ),
        )

    class ExportService:
        settings = SimpleNamespace(output_dir_path=tmp_path)

        def get_export_path(self, trace_id, export_format, pdf_cover_options=None):
            return output_dir / "report.md"

        def get_media_type(self, export_format):
            return "text/markdown"

        def build_download_name(self, trace_id, export_format):
            return "legacy-pg.md"

    principal = {
        "value": AuthenticatedUser(
            id=owner, username=owner, display_name=None, role="user",
            is_active=True, created_at="", updated_at="",
        )
    }
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: principal["value"]
    app.dependency_overrides[adapter.get_legacy_harness_context] = lambda: None
    app.dependency_overrides[deps.get_database_service] = lambda: SimpleNamespace(
        connection=store.connection,
    )
    app.dependency_overrides[get_report_export_service] = ExportService
    monkeypatch.setattr(deps, "get_settings", lambda: SimpleNamespace(output_dir_path=tmp_path))

    with TestClient(app) as client:
        owner_response = client.get(f"/api/v1/reports/{trace_id}/exports/md")
        principal["value"] = AuthenticatedUser(
            id=other, username=other, display_name=None, role="user",
            is_active=True, created_at="", updated_at="",
        )
        other_response = client.get(f"/api/v1/reports/{trace_id}/exports/md")

    assert owner_response.status_code == 200
    assert owner_response.text == "# PostgreSQL 持久归属"
    assert other_response.status_code == 404
    assert other_response.json()["error"]["code"] == "legacy_export_owner_required"


def test_engineering_result_is_not_returned_as_legacy_success(monkeypatch):
    service = FakeHarnessService(formal=False)
    old_service = OldServiceBomb()
    monkeypatch.setattr(adapter, "EvidenceStore", FakeEvidenceStore)
    app = _app(service, old_service)

    with TestClient(app) as client:
        response = client.post("/api/v1/reports/generate", json=_payload())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "legacy_formal_success_unavailable"
    assert old_service.calls == 0


def test_online_legacy_sse_replays_harness_events_and_final(monkeypatch):
    service = FakeHarnessService()
    old_service = OldServiceBomb()
    monkeypatch.setattr(adapter, "EvidenceStore", FakeEvidenceStore)
    app = _app(service, old_service)

    with TestClient(app) as client:
        response = client.post("/api/v1/reports/generate/stream", json=_payload())

    assert response.status_code == 200
    assert "event: stage" in response.text
    assert '"stage": "preparing"' in response.text
    assert "event: final" in response.text
    assert '"status": "success"' in response.text
    assert old_service.calls == 0


@pytest.mark.parametrize("blocked_reason", [None, "resource_pressure", "resource_probe_failed"])
def test_online_runtime_failure_does_not_fall_back(monkeypatch, blocked_reason):
    old_service = OldServiceBomb()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.state.report_harness_runtime = None
    app.state.report_harness_blocked_reason = blocked_reason
    app.dependency_overrides[get_report_service] = lambda: old_service
    app.dependency_overrides[get_optional_current_user] = _user
    app.dependency_overrides[get_user_capability_config_service] = lambda: object()
    monkeypatch.setattr(
        deps, "get_settings",
        lambda: SimpleNamespace(report_harness=ReportHarnessSettings(enabled=True, online_enabled=True)),
    )

    with TestClient(app) as client:
        response = client.post("/api/v1/reports/generate", json=_payload())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == (
        blocked_reason or "outbound_transport_unavailable"
    )
    assert old_service.calls == 0


def test_online_legacy_real_postgres_path_freezes_and_runs_through_harness(
    pg_store, monkeypatch,
):
    store, owner, _, session_id = pg_store
    catalog = AuthorizationCatalog([
        EndpointDescription(
            role="generator", label="合成生成", base_url="https://generator.invalid",
            model="synthetic-generator", version="proof-generator",
        ),
        EndpointDescription(
            role="reviewer", label="合成审查", base_url="https://reviewer.invalid",
            model="synthetic-reviewer", version="proof-reviewer",
        ),
    ], [], frozenset({canonical_digest([])}))
    run_dependencies = replace(
        dependencies(SyntheticRoles()),
        endpoint_profile_digest=catalog.endpoint_digest,
        knowledge_manifest_digest=catalog.knowledge_digest,
        authorization_catalog=catalog,
    )
    service = ReportRunService(store, run_dependencies)
    old_service = OldServiceBomb()
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.state.report_harness_runtime = SimpleNamespace(production_outbound_enabled=True)
    app.dependency_overrides[deps.get_database_service] = lambda: SimpleNamespace(
        connection=store.connection,
    )
    app.dependency_overrides[deps.get_settings] = lambda: SimpleNamespace(
        report_harness=ReportHarnessSettings(enabled=True, online_enabled=True),
    )
    app.dependency_overrides[get_report_service] = lambda: old_service
    app.dependency_overrides[get_optional_current_user] = lambda: AuthenticatedUser(
        id=owner, username=owner, display_name=None, role="user", is_active=True,
        created_at="", updated_at="",
    )
    app.dependency_overrides[get_user_capability_config_service] = lambda: object()
    monkeypatch.setattr(
        adapter, "ReportRunService",
        lambda selected_store, runtime: service
        if isinstance(selected_store, RunStore) and runtime.production_outbound_enabled
        else None,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/v1/reports/generate",
            json={
                "session_id": session_id,
                "accident_data": {"事实": "独立 PG 旧 API harness 接线"},
            },
            headers={"Idempotency-Key": "pg-legacy-run-1"},
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "legacy_formal_success_unavailable"
    assert old_service.calls == 0
    run_id = response.json()["error"]["details"]["run_id"]
    run = service.get(owner, run_id)
    assert run["session_id"] == session_id
    assert run["state"] == "published"
    assert run["snapshot_digest"]
    assert run["formal_export_eligible"] is False
