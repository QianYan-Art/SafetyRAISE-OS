import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import deps, routes_input
from app.core.exceptions import WorkflowError
from app.core.settings import InputGenerationUploadSettings
from app.services.chat_session_service import ChatSessionService


def test_http_late_input_result_cannot_leave_recreated_session_tree(pg_store, tmp_path):
    store, owner, _, session_id = pg_store
    settings = SimpleNamespace(
        chat_sessions_dir_path=tmp_path / "sessions",
        backend_data_dir_path=tmp_path,
        input_generation_workspace_dir_path=tmp_path / "inputs",
        output_dir_path=tmp_path / "outputs",
        input_generation=SimpleNamespace(upload=InputGenerationUploadSettings()),
    )
    settings.output_dir_path.mkdir()
    sessions = object.__new__(ChatSessionService)
    sessions.settings = settings
    sessions.current_user = SimpleNamespace(id=owner, username=owner)
    sessions.database_service = SimpleNamespace(connection=store.connection)
    sessions._history_retriever = None
    sessions._history_retriever_ready = False

    class Input:
        def __init__(self):
            self.settings = settings

        def generate_from_media(self, **kwargs):
            workspace = kwargs["workspace_dir"]
            sessions.delete_session(session_id)
            # 模拟上游识别在删除之后才写回；不调用模型。
            late = workspace / "frames"
            late.mkdir(parents=True)
            (late / "late.jpg").write_bytes(b"test-late-result")
            return None

    app = FastAPI()
    app.include_router(routes_input.router)
    app.dependency_overrides[deps.get_input_generation_service] = Input
    app.dependency_overrides[deps.get_current_user] = lambda: sessions.current_user
    app.dependency_overrides[deps.get_authed_chat_session_service] = lambda: sessions

    @app.exception_handler(WorkflowError)
    async def error(_request, exc):
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code})

    manifest = {
        "groups": [{"category_id": "scene", "category_label": "现场"}],
        "items": [{"category_id": "scene", "media_type": "image", "original_name": "sample.png"}],
    }
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/inputs/generate-from-upload",
            data={"session_id": session_id, "upload_manifest": json.dumps(manifest)},
            files={"files": ("sample.png", b"isolated-input-test", "image/png")},
        )
    assert response.status_code == 404
    assert not (settings.chat_sessions_dir_path / session_id).exists()
