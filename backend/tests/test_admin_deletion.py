import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import deps, routes_admin
from app.core.exceptions import WorkflowError
from app.services.admin_service import AdminService
from app.services.chat_session_service import ChatSessionService


@pytest.fixture
def admin_deletion_client(pg_store, tmp_path, monkeypatch):
    store, owner, other, session_id = pg_store
    settings = SimpleNamespace(
        chat_sessions_dir_path=tmp_path / "sessions",
        backend_data_dir_path=tmp_path,
        input_generation_workspace_dir_path=tmp_path / "inputs",
        output_dir_path=tmp_path / "outputs",
        auth=SimpleNamespace(bootstrap_admin_username="bootstrap"),
    )
    for name in ("sessions", "inputs", "outputs"):
        (tmp_path / name).mkdir()

    def session_service(*, settings):
        service = object.__new__(ChatSessionService)
        service.settings = settings
        service.current_user = None
        service.database_service = SimpleNamespace(connection=store.connection)
        service._history_retriever = None
        service._history_retriever_ready = False
        return service

    monkeypatch.setattr("app.services.admin_service.ChatSessionService", session_service)
    admin = object.__new__(AdminService)
    admin.settings = settings
    admin.database_service = SimpleNamespace(connection=store.connection)
    app = FastAPI()
    app.include_router(routes_admin.router)
    app.dependency_overrides[deps.get_admin_service] = lambda: admin
    app.dependency_overrides[deps.require_admin_user] = lambda: SimpleNamespace(id=other)

    @app.exception_handler(WorkflowError)
    async def error(_request, exc):
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})

    with TestClient(app) as client:
        yield client, store, owner, session_id, settings


def _media(settings, session_id):
    path = settings.chat_sessions_dir_path / session_id / "input_generation" / "batch"
    path.mkdir(parents=True)
    (path / "video.mp4").write_bytes(b"isolated-test-video")
    external = settings.input_generation_workspace_dir_path / ("input-" + uuid4().hex)
    external.mkdir()
    (external / ".session-owner.json").write_text(json.dumps({"session_id": session_id}), "utf-8")
    (external / "frame.jpg").write_bytes(b"isolated-test-frame")
    return path, external


def test_http_admin_delete_space_cleans_files(admin_deletion_client):
    client, store, _, session_id, settings = admin_deletion_client
    paths = _media(settings, session_id)
    assert client.delete(f"/api/v1/admin/spaces/{session_id}").status_code == 200
    assert all(not path.exists() for path in paths)
    with store.connection() as conn:
        assert not conn.execute("SELECT 1 FROM chat_sessions WHERE id=%s", (session_id,)).fetchone()


def test_http_admin_delete_user_cleans_username_legacy_spaces(admin_deletion_client):
    client, store, owner, session_id, settings = admin_deletion_client
    legacy_id = "legacy-" + uuid4().hex
    with store.connection() as conn:
        conn.execute("INSERT INTO chat_sessions(id,owner_username) VALUES (%s,%s)", (legacy_id, owner))
    paths = _media(settings, session_id) + _media(settings, legacy_id)
    unbound = settings.input_generation_workspace_dir_path / "input-unbound"
    unbound.mkdir()
    (unbound / ".session-owner.json").write_text(json.dumps({"session_id": None, "owner_user_id": owner}), "utf-8")
    (unbound / "source.jpg").write_bytes(b"unbound-test-only")
    assert client.delete(f"/api/v1/admin/users/{owner}").status_code == 200
    assert all(not path.exists() for path in paths)
    assert not unbound.exists()
    with store.connection() as conn:
        assert not conn.execute("SELECT 1 FROM chat_sessions WHERE id=%s", (legacy_id,)).fetchone()
        assert not conn.execute("SELECT 1 FROM users WHERE id=%s", (owner,)).fetchone()


def test_http_orphan_cleanup_preserves_username_owned_spaces(admin_deletion_client):
    client, store, owner, _, settings = admin_deletion_client
    orphan_id = "orphan-" + uuid4().hex
    legacy_id = "legacy-" + uuid4().hex
    with store.connection() as conn:
        conn.execute("INSERT INTO chat_sessions(id) VALUES (%s)", (orphan_id,))
        conn.execute("INSERT INTO chat_sessions(id,owner_username) VALUES (%s,%s)", (legacy_id, owner))
    orphan_paths = _media(settings, orphan_id)
    legacy_paths = _media(settings, legacy_id)
    try:
        response = client.post("/api/v1/admin/spaces/cleanup-orphans")
        assert response.status_code == 200
        assert response.json()["deleted_count"] == 1
        assert all(not path.exists() for path in orphan_paths)
        assert all(path.exists() for path in legacy_paths)
    finally:
        with store.connection() as conn:
            conn.execute("DELETE FROM chat_sessions WHERE id IN (%s,%s)", (orphan_id, legacy_id))


def test_http_admin_cleanup_failure_keeps_user_for_retry(admin_deletion_client, monkeypatch):
    client, store, owner, session_id, settings = admin_deletion_client
    paths = _media(settings, session_id)
    import app.services.chat_session_service as module
    original = module.shutil.rmtree
    def fail(_path):
        raise PermissionError("测试锁定")
    monkeypatch.setattr(module.shutil, "rmtree", fail)
    assert client.delete(f"/api/v1/admin/users/{owner}").status_code == 503
    with store.connection() as conn:
        assert conn.execute("SELECT 1 FROM users WHERE id=%s", (owner,)).fetchone()
        assert conn.execute("SELECT 1 FROM chat_sessions WHERE id=%s", (session_id,)).fetchone()
    monkeypatch.setattr(module.shutil, "rmtree", original)
    assert client.delete(f"/api/v1/admin/users/{owner}").status_code == 200
    assert all(not path.exists() for path in paths)
