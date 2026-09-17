from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app.api.deps import get_authed_chat_session_service
from app.api.routes_chat_sessions import router
from app.core.exceptions import SessionNotFoundError
from app.report_harness.errors import HarnessError
from app.report_harness.session_deletion import lock_session_identity
from app.schemas.chat_session import ChatSessionLinkedFile, ChatSessionRecord
from app.services.chat_session_service import ChatSessionService


def _run_document(session_id: str) -> dict:
    return {
        "run_id": str(uuid4()),
        "session_id": session_id,
        "evidence_revision": 0,
        "parent_run_id": None,
    }


def _insert_request(
    store,
    *,
    run_id: str,
    status: str,
    role: str = "generator",
    reserved_tokens: int = 17,
) -> tuple[str, str]:
    request_id = uuid4()
    attempt_id = uuid4()
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO report_run_requests "
            "(request_id,run_id,attempt_id,fencing_token,role,endpoint_digest,"
            "request_digest,status,reserved_tokens,actual_tokens,result) "
            "VALUES (%s,%s,%s,0,%s,%s,%s,%s,%s,NULL,NULL)",
            (
                request_id,
                run_id,
                attempt_id,
                role,
                "e" * 64,
                "r" * 64,
                status,
                reserved_tokens,
            ),
        )
    return str(request_id), str(attempt_id)


def _service_for_session(
    store,
    owner: str,
    session_id: str,
    root: Path,
    record: ChatSessionRecord | None = None,
):
    service = object.__new__(ChatSessionService)
    service.database_service = SimpleNamespace(connection=store.connection)
    service.current_user = SimpleNamespace(id=owner, username=owner)
    service.settings = SimpleNamespace(
        chat_sessions_dir_path=root,
        backend_data_dir_path=root,
    )
    record = record or ChatSessionRecord(id=session_id, created_at=1, updated_at=1)
    service.get_session = lambda _session_id: record
    return service


def test_delete_session_cancels_runs_and_preserves_ledger(pg_store, tmp_path):
    store, owner, _, session_id = pg_store
    document = _run_document(session_id)
    run = store.create(owner, uuid4(), "q" * 64, document)
    intent_id, _ = _insert_request(
        store, run_id=run["run_id"], status="intent", reserved_tokens=11,
    )
    dispatched_id, _ = _insert_request(
        store, run_id=run["run_id"], status="dispatched", reserved_tokens=13,
    )
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO report_session_evidence(session_id,owner_user_id,revision,records) "
            "VALUES (%s,%s,0,%s)",
            (session_id, owner, Jsonb([])),
        )

    session_dir = tmp_path / session_id
    session_dir.mkdir()
    (session_dir / "session.json").write_text("{}", encoding="utf-8")
    service = _service_for_session(store, owner, session_id, tmp_path)

    service.delete_session(session_id)

    with store.connection() as conn:
        barrier = conn.execute(
            "SELECT session_id FROM session_deletion_barriers WHERE session_id=%s",
            (session_id,),
        ).fetchone()
        run_row = conn.execute(
            "SELECT state,state_version,last_event_seq,deleted_at,fencing_token,"
            "lease_owner,lease_expires_at,document FROM report_runs WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()
        statuses = conn.execute(
            "SELECT request_id,status FROM report_run_requests WHERE run_id=%s "
            "ORDER BY request_id",
            (run["run_id"],),
        ).fetchall()
        event = conn.execute(
            "SELECT type,state_version,data FROM report_run_events WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM report_session_evidence WHERE session_id=%s",
            (session_id,),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM chat_sessions WHERE id=%s", (session_id,),
        ).fetchone() is None

    assert barrier["session_id"] == session_id
    assert run_row["state"] == "cancelled"
    assert run_row["state_version"] == 1
    assert run_row["last_event_seq"] == 1
    assert run_row["deleted_at"] is not None
    assert run_row["fencing_token"] == 1
    assert run_row["lease_owner"] is None
    assert run_row["lease_expires_at"] is None
    assert run_row["document"] == {
        **document,
        "active_seconds": 0.0,
        "active_started_at": None,
        "terminal_reason": "session_deleted",
    }
    assert {str(row["request_id"]): row["status"] for row in statuses} == {
        intent_id: "rejected",
        dispatched_id: "completion_unknown",
    }
    assert event["type"] == "final"
    assert event["state_version"] == 1
    assert event["data"] == {"state": "cancelled", "reason": "session_deleted"}
    assert not session_dir.exists()
    with pytest.raises(HarnessError, match="not_found"):
        store.get(owner, run["run_id"])


def test_delete_session_preserves_terminal_document_and_ledger(pg_store, tmp_path):
    store, owner, _, session_id = pg_store
    document = {**_run_document(session_id), "final_text": "保留", "state_marker": 7}
    run = store.create(owner, uuid4(), "q" * 64, document)
    request_id, _ = _insert_request(
        store, run_id=run["run_id"], status="committed", reserved_tokens=9,
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET state='published',document=%s,"
            "state_version=4,last_event_seq=3,lease_owner=NULL,"
            "lease_expires_at=NULL WHERE run_id=%s",
            (Jsonb(document), run["run_id"]),
        )

    service = _service_for_session(store, owner, session_id, tmp_path)
    service.delete_session(session_id)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT state,state_version,last_event_seq,deleted_at,fencing_token,document "
            "FROM report_runs WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()
        request = conn.execute(
            "SELECT request_id,status FROM report_run_requests WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()
        assert conn.execute(
            "SELECT count(*) AS count FROM report_run_events WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()["count"] == 0

    assert str(request["request_id"]) == request_id
    assert request["status"] == "committed"
    assert row["state"] == "published"
    assert row["state_version"] == 4
    assert row["last_event_seq"] == 3
    assert row["deleted_at"] is not None
    assert row["fencing_token"] == 1
    assert row["document"] == document


def test_delete_active_run_freezes_elapsed_time_in_document(pg_store, tmp_path):
    store, owner, _, session_id = pg_store
    document = {
        **_run_document(session_id),
        "active_seconds": 4.0,
        "active_started_at": "2020-01-01T00:00:00+00:00",
        "body": "保留正文",
    }
    run = store.create(owner, uuid4(), "q" * 64, document)
    with store.connection() as conn:
        conn.execute(
            "UPDATE report_runs SET state='generating',state_version=2,"
            "last_event_seq=1,fencing_token=3,lease_owner=%s,"
            "lease_expires_at=clock_timestamp()+interval '30 seconds',document=%s "
            "WHERE run_id=%s",
            (uuid4(), Jsonb(document), run["run_id"]),
        )

    service = _service_for_session(store, owner, session_id, tmp_path)
    service.delete_session(session_id)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT state,state_version,fencing_token,document FROM report_runs "
            "WHERE run_id=%s",
            (run["run_id"],),
        ).fetchone()

    assert row["state"] == "cancelled"
    assert row["state_version"] == 3
    assert row["fencing_token"] == 4
    assert row["document"]["body"] == "保留正文"
    assert row["document"]["active_started_at"] is None
    assert row["document"]["terminal_reason"] == "session_deleted"
    assert row["document"]["active_seconds"] > document["active_seconds"]


def test_delete_session_enforces_existing_owner_in_transaction(pg_store, tmp_path):
    store, owner, other, session_id = pg_store
    service = _service_for_session(store, other, session_id, tmp_path)

    with pytest.raises(SessionNotFoundError):
        service.delete_session(session_id)

    with store.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM session_deletion_barriers WHERE session_id=%s",
            (session_id,),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM chat_sessions WHERE id=%s", (session_id,),
        ).fetchone() is not None


def test_http_delete_path_uses_transactional_service(pg_store, tmp_path):
    store, owner, _, session_id = pg_store
    service = _service_for_session(store, owner, session_id, tmp_path)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_authed_chat_session_service] = lambda: service

    with TestClient(app) as client:
        response = client.delete(f"/api/v1/chat-sessions/{session_id}")

    assert response.status_code == 200
    assert response.json() == {"status": "success"}
    with store.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM session_deletion_barriers WHERE session_id=%s",
            (session_id,),
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM chat_sessions WHERE id=%s", (session_id,),
        ).fetchone() is None


def test_deleted_session_blocks_stale_write_and_legacy_file_migration(
    pg_store, tmp_path,
):
    store, owner, _, session_id = pg_store
    legacy_id = "legacy-" + uuid4().hex
    legacy_dir = tmp_path / legacy_id
    legacy_dir.mkdir()
    legacy_record = ChatSessionRecord(id=legacy_id, created_at=1, updated_at=1)
    (legacy_dir / "session.json").write_text(
        json.dumps(legacy_record.model_dump(), ensure_ascii=False),
        encoding="utf-8",
    )
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO session_deletion_barriers(session_id) VALUES (%s)",
            (session_id,),
        )
        conn.execute(
            "INSERT INTO session_deletion_barriers(session_id) VALUES (%s)",
            (legacy_id,),
        )

    service = _service_for_session(store, owner, session_id, tmp_path)
    try:
        with pytest.raises(SessionNotFoundError):
            service._write_session(legacy_record)
        service._migrate_legacy_file_sessions()
        with store.connection() as conn:
            assert conn.execute(
                "SELECT 1 FROM chat_sessions WHERE id=%s", (legacy_id,),
            ).fetchone() is None
    finally:
        with store.connection() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM session_deletion_barriers WHERE session_id IN (%s,%s)",
                (session_id, legacy_id),
            )
        if legacy_dir.exists():
            for path in legacy_dir.iterdir():
                path.unlink()
            legacy_dir.rmdir()


def test_cross_connection_late_draft_write_cannot_recreate_deleted_session(
    pg_store, tmp_path,
):
    store, owner, _, session_id = pg_store
    target = tmp_path / session_id / "generated-input.json"
    record = ChatSessionRecord(
        id=session_id,
        created_at=1,
        updated_at=1,
        draft_json=json.dumps({"事故": "迟到写入"}, ensure_ascii=False),
        draft_meta={"input_path": str(target)},
    )
    stale_service = _service_for_session(store, owner, session_id, tmp_path, record)
    connection_ready = Event()
    finished = Event()
    outcome: dict[str, BaseException] = {}

    @contextmanager
    def tracked_connection():
        with store.connection() as conn:
            connection_ready.set()
            yield conn

    stale_service.database_service = SimpleNamespace(connection=tracked_connection)

    def late_write() -> None:
        try:
            stale_service._sync_draft_artifacts(record)
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc
        finally:
            finished.set()

    with store.connection() as blocker, blocker.transaction():
        lock_session_identity(blocker, session_id)
        thread = Thread(target=late_write)
        thread.start()
        assert connection_ready.wait(timeout=5)
        blocker.execute(
            "INSERT INTO session_deletion_barriers(session_id) VALUES (%s) "
            "ON CONFLICT (session_id) DO NOTHING",
            (session_id,),
        )

    assert finished.wait(timeout=5)
    thread.join(timeout=5)
    assert isinstance(outcome.get("error"), SessionNotFoundError)
    assert not target.exists()

    with store.connection() as conn, conn.transaction():
        conn.execute(
            "DELETE FROM session_deletion_barriers WHERE session_id=%s",
            (session_id,),
        )


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows 文件系统大小写等价规则")
def test_casefolded_late_draft_write_cannot_recreate_deleted_session(
    pg_store, tmp_path,
):
    store, owner, _, session_id = pg_store
    alias_id = session_id.upper()
    target = tmp_path / alias_id / "generated-input.json"
    record = ChatSessionRecord(
        id=alias_id,
        created_at=1,
        updated_at=1,
        draft_json=json.dumps({"事故": "大小写别名迟到写入"}, ensure_ascii=False),
        draft_meta={"input_path": str(target)},
    )
    stale_service = _service_for_session(store, owner, alias_id, tmp_path, record)
    connection_ready = Event()
    finished = Event()
    outcome: dict[str, BaseException] = {}

    @contextmanager
    def tracked_connection():
        with store.connection() as conn:
            connection_ready.set()
            yield conn

    stale_service.database_service = SimpleNamespace(connection=tracked_connection)

    def late_write() -> None:
        try:
            stale_service._sync_draft_artifacts(record)
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc
        finally:
            finished.set()

    try:
        with store.connection() as blocker, blocker.transaction():
            lock_session_identity(blocker, session_id)
            thread = Thread(target=late_write)
            thread.start()
            assert connection_ready.wait(timeout=5)
            blocker.execute(
                "INSERT INTO session_deletion_barriers(session_id) VALUES (%s) "
                "ON CONFLICT (session_id) DO NOTHING",
                (session_id,),
            )

        assert finished.wait(timeout=5)
        thread.join(timeout=5)
        assert isinstance(outcome.get("error"), SessionNotFoundError)
        assert not target.exists()
    finally:
        with store.connection() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM session_deletion_barriers WHERE session_id=%s",
                (session_id,),
            )


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows 文件系统大小写等价规则")
def test_casefolded_live_session_id_cannot_write_shared_path_or_metadata(
    pg_store, tmp_path,
):
    store, owner, _, session_id = pg_store
    alias_id = session_id.upper()
    target = tmp_path / alias_id / "generated-input.json"
    record = ChatSessionRecord(
        id=alias_id,
        created_at=1,
        updated_at=1,
        draft_json=json.dumps({"事故": "大小写别名"}, ensure_ascii=False),
        draft_meta={"input_path": str(target)},
    )
    service = _service_for_session(store, owner, alias_id, tmp_path, record)

    with pytest.raises(SessionNotFoundError, match="标识冲突"):
        service._sync_draft_artifacts(record)
    with pytest.raises(SessionNotFoundError, match="标识冲突"):
        service._write_session(record)

    assert not target.exists()
    with store.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM chat_sessions WHERE id=%s", (alias_id,),
        ).fetchone() is None


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows 文件系统大小写等价规则")
def test_casefolded_session_id_cannot_delete_other_session_tree(pg_store, tmp_path):
    store, owner, _, session_id = pg_store
    alias_id = session_id.upper()
    session_dir = tmp_path / session_id
    session_dir.mkdir()
    sentinel = session_dir / "sentinel.txt"
    sentinel.write_text("保留", encoding="utf-8")
    record = ChatSessionRecord(id=alias_id, created_at=1, updated_at=1)
    service = _service_for_session(store, owner, alias_id, tmp_path, record)

    with pytest.raises(SessionNotFoundError, match="标识冲突"):
        service.delete_session(alias_id)

    assert sentinel.read_text(encoding="utf-8") == "保留"
    with store.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM chat_sessions WHERE id=%s", (session_id,),
        ).fetchone() is not None


def test_delete_only_removes_current_session_descendants(pg_store, tmp_path):
    store, owner, _, session_id = pg_store
    other_id = "other-" + uuid4().hex
    current_dir = tmp_path / session_id
    other_dir = tmp_path / other_id
    current_dir.mkdir()
    other_dir.mkdir()
    owned_dir = current_dir / "owned"
    owned_dir.mkdir()
    (owned_dir / "artifact.txt").write_text("当前会话", encoding="utf-8")
    other_sentinel = other_dir / "sentinel.txt"
    other_sentinel.write_text("其他会话", encoding="utf-8")
    root_sentinel = tmp_path / "root-sentinel.txt"
    root_sentinel.write_text("根目录", encoding="utf-8")
    record = ChatSessionRecord(
        id=session_id,
        created_at=1,
        updated_at=1,
        linked_files=[
            ChatSessionLinkedFile(
                label="恶意其他会话目录",
                path=str(other_dir),
                category="malicious",
                path_type="dir",
            ),
            ChatSessionLinkedFile(
                label="恶意根目录",
                path=str(tmp_path),
                category="malicious",
                path_type="dir",
            ),
            ChatSessionLinkedFile(
                label="当前会话产物",
                path=str(owned_dir),
                category="owned",
                path_type="dir",
            ),
        ],
    )
    service = _service_for_session(store, owner, session_id, tmp_path, record)

    service.delete_session(session_id)

    assert not current_dir.exists()
    assert other_dir.exists()
    assert other_sentinel.read_text(encoding="utf-8") == "其他会话"
    assert root_sentinel.read_text(encoding="utf-8") == "根目录"


def test_session_dir_rejects_path_segments_and_preserves_simple_ids(tmp_path):
    service = object.__new__(ChatSessionService)
    service.settings = SimpleNamespace(chat_sessions_dir_path=tmp_path)

    for invalid in (
        "",
        ".",
        "..",
        "../other",
        r"..\other",
        "nested/name",
        r"nested\name",
        str(tmp_path / "absolute"),
        r"C:\absolute",
    ):
        with pytest.raises(SessionNotFoundError):
            service._session_dir(invalid)

    assert service._session_dir("legacy-session-1") == (tmp_path / "legacy-session-1").resolve()


@pytest.mark.skipif(os.name != "nt", reason="仅验证 Windows 文件名组件规则")
def test_session_dir_rejects_windows_special_components_and_preserves_case_ids(tmp_path):
    service = object.__new__(ChatSessionService)
    service.settings = SimpleNamespace(chat_sessions_dir_path=tmp_path)

    for invalid in (
        "session.",
        "session ",
        "draft:stream",
        "CON",
        "con.txt",
        "NUL",
        "COM1",
        "LPT9.log",
        "wild*card",
        "wild?card",
        "wild|card",
        "wild\"card",
        "control\x01",
    ):
        with pytest.raises(SessionNotFoundError):
            service._session_dir(invalid)

    assert service._session_dir("Case-ID-ABC") == (tmp_path / "Case-ID-ABC").resolve()


def test_live_session_cannot_recreate_deleted_session_directory_from_draft_path(
    pg_store, tmp_path,
):
    store, owner, _, deleted_session_id = pg_store
    live_session_id = "live-" + uuid4().hex
    deleted_dir = tmp_path / deleted_session_id
    deleted_dir.mkdir()
    sentinel = deleted_dir / "deleted-sentinel.txt"
    sentinel.write_text("删除前", encoding="utf-8")
    target = deleted_dir / "from-live-session.json"
    live_record = ChatSessionRecord(
        id=live_session_id,
        owner_user_id=owner,
        owner_username=owner,
        created_at=1,
        updated_at=1,
        draft_json=json.dumps({"来源": "存活会话B"}, ensure_ascii=False),
        draft_meta={"input_path": str(target)},
    )
    live_service = _service_for_session(
        store,
        owner,
        live_session_id,
        tmp_path,
        live_record,
    )
    delete_service = _service_for_session(store, owner, deleted_session_id, tmp_path)

    try:
        with store.connection() as conn:
            conn.execute(
                "INSERT INTO chat_sessions(id,owner_user_id,owner_username,draft_json) "
                "VALUES (%s,%s,%s,%s)",
                (
                    live_session_id,
                    owner,
                    owner,
                    live_record.draft_json,
                ),
            )
        delete_service.delete_session(deleted_session_id)
        live_service._sync_draft_artifacts(live_record)

        assert not deleted_dir.exists()
        assert not target.exists()
    finally:
        with store.connection() as conn, conn.transaction():
            conn.execute(
                "DELETE FROM chat_sessions WHERE id=%s",
                (live_session_id,),
            )
            conn.execute(
                "DELETE FROM session_deletion_barriers WHERE session_id=%s",
                (deleted_session_id,),
            )
