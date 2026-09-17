from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator
from uuid import UUID

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.report_harness.errors import HarnessError
from app.report_harness.contracts import canonical_digest
from app.report_harness.evidence import EvidenceBindingError, freeze_snapshot

TERMINAL_STATES = frozenset({"published", "needs_review", "cancelled", "failed"})
NEXT_STATES = {
    "queued": {"queued", "preparing", "cancelled", "failed"},
    "preparing": {"preparing", "generating", "suspended", "needs_review", "cancelled", "failed"},
    "generating": {"generating", "checking", "suspended", "needs_review", "cancelled", "failed"},
    "checking": {"checking", "revising", "published", "suspended", "needs_review", "cancelled", "failed"},
    "revising": {"revising", "checking", "suspended", "needs_review", "cancelled", "failed"},
    "suspended": {"suspended", "preparing", "cancelled", "failed"},
}


class RunStore:
    """借用独立连接边界；不读取配置、不自动迁移或连接业务库。"""

    def __init__(self, connection: Callable):
        self.connection = connection

    def check_schema(self) -> None:
        with self.connection() as conn:
            conn.row_factory = dict_row
            row = conn.execute(
                "SELECT to_regclass('report_run_schema_version') AS relation"
            ).fetchone()
            if not row or row["relation"] is None:
                raise HarnessError("schema_unavailable", 503)
            versions = conn.execute(
                "SELECT version FROM report_run_schema_version ORDER BY version"
            ).fetchall()
            if [v["version"] for v in versions] != [1, 2]:
                raise HarnessError("schema_version_mismatch", 503)

    @staticmethod
    def _owned_session(conn: Connection, owner: str, session_id: str) -> dict:
        row = conn.execute(
            "SELECT id, draft_json FROM chat_sessions "
            "WHERE id = %s AND (owner_user_id = %s OR "
            "(owner_user_id IS NULL AND owner_username="
            "(SELECT username FROM users WHERE id=%s))) "
            "AND NOT EXISTS (SELECT 1 FROM session_deletion_barriers b "
            "WHERE b.session_id=chat_sessions.id) FOR UPDATE",
            (session_id, owner, owner),
        ).fetchone()
        if row is None:
            raise HarnessError("not_found", 404)
        return row

    def create(
        self, owner: str, request_id: UUID, request_digest: str, document: dict
    ) -> dict:
        try:
            with self.connection() as conn, conn.transaction():
                conn.row_factory = dict_row
                self._owned_session(conn, owner, document["session_id"])
                existing = conn.execute(
                    "SELECT * FROM report_runs WHERE owner_user_id=%s AND request_id=%s",
                    (owner, request_id),
                ).fetchone()
                if existing:
                    if existing["request_digest"] != request_digest:
                        raise HarnessError("idempotency_conflict")
                    if existing["deleted_at"] is not None:
                        raise HarnessError("not_found", 404)
                    return self._view(existing)
                evidence = conn.execute(
                    "SELECT revision, records FROM report_session_evidence "
                    "WHERE session_id=%s AND owner_user_id=%s",
                    (document["session_id"], owner),
                ).fetchone()
                revision = evidence["revision"] if evidence else 0
                if document["evidence_revision"] != revision:
                    raise HarnessError("evidence_revision_conflict")
                if "snapshot" in document:
                    try:
                        snapshot = freeze_snapshot(
                            document["snapshot"]["accident_data"],
                            evidence["records"] if evidence else [], revision,
                            document["snapshot"]["knowledge_manifest_digest"],
                        )
                    except EvidenceBindingError as exc:
                        raise HarnessError("invalid_field_bindings", 422,
                                           {"field_errors": exc.warnings}) from exc
                    document = {**document, "snapshot": snapshot,
                                "snapshot_digest": canonical_digest(snapshot)}
                parent = document.get("parent_run_id")
                if parent:
                    if not conn.execute(
                        "SELECT 1 FROM report_runs WHERE run_id=%s AND owner_user_id=%s "
                        "AND session_id=%s AND deleted_at IS NULL",
                        (parent, owner, document["session_id"]),
                    ).fetchone():
                        raise HarnessError("not_found", 404)
                row = conn.execute(
                    "INSERT INTO report_runs "
                    "(run_id,owner_user_id,session_id,request_id,request_digest,state,document) "
                    "VALUES (%s,%s,%s,%s,%s,'queued',%s) RETURNING *",
                    (document["run_id"], owner, document["session_id"],
                     request_id, request_digest, Jsonb(document)),
                ).fetchone()
                return self._view(row)
        except UniqueViolation as exc:
            if exc.diag.constraint_name == "report_runs_owner_request":
                with self.connection() as conn:
                    conn.row_factory = dict_row
                    existing = conn.execute(
                        "SELECT * FROM report_runs WHERE owner_user_id=%s AND request_id=%s",
                        (owner, request_id),
                    ).fetchone()
                    if existing and existing["request_digest"] != request_digest:
                        raise HarnessError("idempotency_conflict") from exc
                    if existing and existing["deleted_at"] is None:
                        return self._view(existing)
                    raise HarnessError("not_found", 404) from exc
            if exc.diag.constraint_name == "report_runs_active_session":
                raise HarnessError("active_run_conflict") from exc
            raise

    @staticmethod
    def _view(row: dict) -> dict:
        return {
            **row["document"],
            "active_seconds": RunStore._active_seconds(row),
            "run_id": str(row["run_id"]),
            "state": row["state"],
            "state_version": row["state_version"],
            "last_event_seq": row["last_event_seq"],
        }

    @staticmethod
    def _active_seconds(row: dict) -> float:
        document = row["document"]
        total = float(document.get("active_seconds", 0))
        started = document.get("active_started_at")
        if started and row.get("db_now") is not None:
            total += max(0, (row["db_now"] - datetime.fromisoformat(started)).total_seconds())
        return total

    @staticmethod
    def _read(conn: Connection, owner: str, run_id: str, *, lock=False) -> dict:
        row = conn.execute(
            "SELECT r.*,clock_timestamp() AS db_now FROM report_runs r "
            "JOIN chat_sessions s ON s.id=r.session_id "
            "WHERE r.run_id=%s AND r.owner_user_id=%s AND "
            "(s.owner_user_id=%s OR (s.owner_user_id IS NULL AND s.owner_username="
            "(SELECT username FROM users WHERE id=%s))) "
            "AND r.deleted_at IS NULL AND NOT EXISTS "
            "(SELECT 1 FROM session_deletion_barriers b WHERE b.session_id=r.session_id)"
            + (" FOR UPDATE OF r" if lock else ""),
            (run_id, owner, owner, owner),
        ).fetchone()
        if row is None:
            raise HarnessError("not_found", 404)
        if lock:
            # 等待行锁后重新取时钟，不能拿等待前的时间判断租约仍有效。
            row["db_now"] = conn.execute("SELECT clock_timestamp() AS db_now").fetchone()["db_now"]
        return row

    def get(self, owner: str, run_id: str) -> dict:
        with self.connection() as conn:
            conn.row_factory = dict_row
            return self._view(self._read(conn, owner, run_id))

    @contextmanager
    def locked_settlement(self, owner: str, run_id: str) -> Iterator[tuple[Connection, dict]]:
        """仅内部原 attempt 结算可越过会话删除屏障，不授予运行读写或发送权限。"""
        with self.connection() as conn, conn.transaction():
            conn.row_factory = dict_row
            row = conn.execute(
                "SELECT * FROM report_runs WHERE run_id=%s AND owner_user_id=%s FOR UPDATE",
                (run_id, owner),
            ).fetchone()
            if row is None:
                raise HarnessError("not_found", 404)
            yield conn, row

    @contextmanager
    def locked(
        self, owner: str, run_id: str, expected_version: int | None = None,
        fencing_token: int | None = None,
    ) -> Iterator[tuple[Connection, dict]]:
        with self.connection() as conn, conn.transaction():
            conn.row_factory = dict_row
            row = self._read(conn, owner, run_id, lock=True)
            if expected_version is not None and row["state_version"] != expected_version:
                raise HarnessError("version_conflict")
            if fencing_token is not None:
                expiry = row["lease_expires_at"]
                if (row["fencing_token"] != fencing_token or expiry is None
                        or expiry <= row["db_now"]
                        or row["state"] in TERMINAL_STATES):
                    raise HarnessError("lease_lost")
            yield conn, row

    def acquire(self, owner: str, run_id: str, expected_version: int, worker: UUID) -> int:
        with self.locked(owner, run_id, expected_version) as (conn, row):
            if row["state"] != "queued":
                raise HarnessError("not_executable")
            token = row["fencing_token"] + 1
            conn.execute(
                "UPDATE report_runs SET fencing_token=%s,lease_owner=%s,"
                "lease_expires_at=clock_timestamp()+interval '30 seconds' WHERE run_id=%s",
                (token, worker, run_id),
            )
            self.save(conn, row, "preparing", {
                **row["document"], "active_started_at": row["db_now"].isoformat(),
            }, "stage", {"stage": "preparing"})
            return token

    def heartbeat(self, owner: str, run_id: str, token: int) -> None:
        with self.locked(owner, run_id, fencing_token=token) as (conn, _):
            conn.execute(
                "UPDATE report_runs SET lease_expires_at="
                "clock_timestamp()+interval '30 seconds' WHERE run_id=%s", (run_id,),
            )

    def assert_active(self, owner: str, run_id: str, token: int) -> None:
        with self.locked(owner, run_id, fencing_token=token):
            return

    @staticmethod
    def save(conn: Connection, row: dict, state: str, document: dict,
             event_type: str, data: dict) -> dict:
        if row["state"] in TERMINAL_STATES:
            raise HarnessError("terminal_run")
        if state not in NEXT_STATES[row["state"]]:
            raise HarnessError("invalid_state_transition")
        seq = row["last_event_seq"] + 1
        version = row["state_version"] + 1
        terminal = state in TERMINAL_STATES
        if terminal or state == "suspended":
            document = {
                **document, "active_seconds": RunStore._active_seconds(row),
                "active_started_at": None,
            }
        updated = conn.execute(
            "UPDATE report_runs SET state=%s,state_version=%s,document=%s,last_event_seq=%s,"
            "lease_owner=CASE WHEN %s THEN NULL ELSE lease_owner END,"
            "lease_expires_at=CASE WHEN %s THEN NULL ELSE lease_expires_at END "
            "WHERE run_id=%s AND state_version=%s RETURNING *",
            (state, version, Jsonb(document), seq, terminal, terminal,
             row["run_id"], row["state_version"]),
        ).fetchone()
        if updated is None:
            raise HarnessError("version_conflict")
        conn.execute(
            "INSERT INTO report_run_events(run_id,seq,type,state_version,data) "
            "VALUES (%s,%s,%s,%s,%s)",
            (row["run_id"], seq, event_type, version, Jsonb(data)),
        )
        return RunStore._view({**updated, "db_now": row["db_now"]})

    def transition(self, owner: str, run_id: str, token: int,
                   state: str, patch: dict, event_type="stage", data=None) -> dict:
        with self.locked(owner, run_id, fencing_token=token) as (conn, row):
            return self.save(conn, row, state, {**row["document"], **patch},
                             event_type, data or {"stage": state})

    def cancel(self, owner: str, run_id: str, expected_token: int | None = None) -> dict:
        with self.locked(owner, run_id) as (conn, row):
            if expected_token is not None and row["fencing_token"] != expected_token:
                return self._view(row)
            if row["state"] in TERMINAL_STATES:
                return self._view(row)
            conn.execute(
                "UPDATE report_runs SET fencing_token=fencing_token+1 WHERE run_id=%s",
                (run_id,),
            )
            conn.execute(
                "UPDATE report_run_requests SET status=CASE WHEN status='intent' "
                "THEN 'rejected' ELSE 'completion_unknown' END "
                "WHERE run_id=%s AND status IN ('intent','dispatched')",
                (run_id,),
            )
            return self.save(conn, row, "cancelled",
                             {**row["document"], "terminal_reason": "user_cancelled"},
                             "final", {"state": "cancelled"})

    def events(self, owner: str, run_id: str, after_seq=0, limit=100) -> dict:
        with self.connection() as conn:
            conn.row_factory = dict_row
            self._read(conn, owner, run_id)
            rows = conn.execute(
                "SELECT run_id,seq,type,state_version,occurred_at,data "
                "FROM report_run_events WHERE run_id=%s AND seq>%s ORDER BY seq LIMIT %s",
                (run_id, after_seq, limit),
            ).fetchall()
            return {"events": rows, "next_seq": rows[-1]["seq"] if rows else after_seq}
