from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.core.exceptions import SessionNotFoundError

_ACTIVE_RUN_STATES = frozenset(
    {"queued", "preparing", "generating", "checking", "revising", "suspended"}
)


def _session_identity_key(session_id: str) -> str:
    """按当前文件系统的路径规则生成会话锁键。"""
    return os.path.normcase(session_id)


def lock_session_identity(conn: Any, session_id: str) -> None:
    """用数据库事务级锁串行化同一会话的删除、写回和旧文件迁移。"""
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (_session_identity_key(session_id),),
    )


def relation_exists(conn: Any, relation: str) -> bool:
    row = conn.execute(
        "SELECT to_regclass(%s) AS relation",
        (relation,),
    ).fetchone()
    if row is None:
        return False
    if isinstance(row, dict):
        return row["relation"] is not None
    return row[0] is not None


def is_session_deleted(conn: Any, session_id: str) -> bool:
    if not relation_exists(conn, "session_deletion_barriers"):
        return False
    if os.name == "nt":
        return conn.execute(
            "SELECT 1 FROM session_deletion_barriers WHERE lower(session_id)=lower(%s)",
            (session_id,),
        ).fetchone() is not None
    return conn.execute(
        "SELECT 1 FROM session_deletion_barriers WHERE session_id=%s",
        (session_id,),
    ).fetchone() is not None


def assert_session_not_deleted(conn: Any, session_id: str) -> None:
    if is_session_deleted(conn, session_id):
        raise SessionNotFoundError(f"会话不存在: {session_id}")


def assert_session_identity_available(conn: Any, session_id: str) -> None:
    """拒绝会与其他数据库会话复用同一 Windows 目录的标识。"""
    if os.name != "nt" or not relation_exists(conn, "chat_sessions"):
        return
    row = conn.execute(
        "SELECT id FROM chat_sessions "
        "WHERE lower(id)=lower(%s) AND id<>%s LIMIT 1",
        (session_id, session_id),
    ).fetchone()
    if row is not None:
        raise SessionNotFoundError(f"会话目录标识冲突: {session_id}")


def guarded_session_file_write(
    connection_factory: Callable[[], Any],
    session_id: str,
    writer: Callable[[], Any],
) -> Any:
    """在同一会话的数据库锁和删除屏障检查内执行文件写入。"""
    with connection_factory() as conn, conn.transaction():
        conn.row_factory = dict_row
        lock_session_identity(conn, session_id)
        assert_session_not_deleted(conn, session_id)
        assert_session_identity_available(conn, session_id)
        return writer()


def _lock_owned_session(
    conn: Any,
    session_id: str,
    *,
    owner_user_id: str | None,
    owner_username: str | None,
) -> dict:
    if owner_user_id is None:
        row = conn.execute(
            "SELECT id, owner_user_id, owner_username "
            "FROM chat_sessions WHERE id=%s FOR UPDATE",
            (session_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id, owner_user_id, owner_username "
            "FROM chat_sessions WHERE id=%s AND (owner_user_id=%s OR "
            "(owner_user_id IS NULL AND owner_username=%s)) FOR UPDATE",
            (session_id, owner_user_id, owner_username),
        ).fetchone()
    if row is None:
        raise SessionNotFoundError(f"会话不存在: {session_id}")
    return row


def _cancel_run(conn: Any, row: dict, *, db_now: datetime) -> str | None:
    run_id = str(row["run_id"])
    state = row["state"]
    if state not in _ACTIVE_RUN_STATES:
        conn.execute(
            "UPDATE report_runs SET deleted_at=COALESCE(deleted_at, clock_timestamp()), "
            "fencing_token=fencing_token+1, lease_owner=NULL, lease_expires_at=NULL "
            "WHERE run_id=%s",
            (row["run_id"],),
        )
        return None

    document = dict(row["document"] or {})
    active_seconds = float(document.get("active_seconds", 0) or 0)
    started_at = document.get("active_started_at")
    if started_at:
        started = datetime.fromisoformat(str(started_at))
        if started.tzinfo is None and db_now.tzinfo is not None:
            started = started.replace(tzinfo=db_now.tzinfo)
        active_seconds += max(0.0, (db_now - started).total_seconds())
    document.update(
        {
            "active_seconds": active_seconds,
            "active_started_at": None,
            "terminal_reason": "session_deleted",
        }
    )
    next_version = row["state_version"] + 1
    next_seq = row["last_event_seq"] + 1
    updated = conn.execute(
        "UPDATE report_runs SET state='cancelled', state_version=%s, "
        "last_event_seq=%s, deleted_at=COALESCE(deleted_at, clock_timestamp()), "
        "fencing_token=fencing_token+1, lease_owner=NULL, lease_expires_at=NULL, "
        "document=%s "
        "WHERE run_id=%s AND state_version=%s RETURNING run_id",
        (next_version, next_seq, Jsonb(document), row["run_id"], row["state_version"]),
    ).fetchone()
    if updated is None:
        raise RuntimeError("会话删除时run版本发生并发变化")
    conn.execute(
        "INSERT INTO report_run_events(run_id, seq, type, state_version, data) "
        "VALUES (%s, %s, 'final', %s, %s)",
        (
            row["run_id"],
            next_seq,
            next_version,
            Jsonb({"state": "cancelled", "reason": "session_deleted"}),
        ),
    )
    return run_id


def _close_attempts(conn: Any, run_id: Any) -> dict[str, list[str]]:
    if not relation_exists(conn, "report_run_requests"):
        return {"rejected_request_ids": [], "unknown_request_ids": []}
    rows = conn.execute(
        "SELECT request_id, attempt_id, status FROM report_run_requests "
        "WHERE run_id=%s ORDER BY attempt_id FOR UPDATE",
        (run_id,),
    ).fetchall()
    rejected: list[str] = []
    unknown: list[str] = []
    for row in rows:
        request_id = str(row["request_id"])
        if row["status"] == "intent":
            conn.execute(
                "UPDATE report_run_requests SET status='rejected' "
                "WHERE request_id=%s AND "
                "run_id=%s AND status='intent'",
                (row["request_id"], run_id),
            )
            rejected.append(request_id)
        elif row["status"] == "dispatched":
            conn.execute(
                "UPDATE report_run_requests SET status='completion_unknown' "
                "WHERE request_id=%s AND run_id=%s AND status='dispatched'",
                (row["request_id"], run_id),
            )
            unknown.append(request_id)
    return {"rejected_request_ids": rejected, "unknown_request_ids": unknown}


def delete_session(
    connection_factory: Callable[[], Any],
    session_id: str,
    *,
    owner_user_id: str | None = None,
    owner_username: str | None = None,
) -> dict:
    """在一个事务内建立屏障、撤销run并删除会话持久记录。"""
    with connection_factory() as conn, conn.transaction():
        conn.row_factory = dict_row
        lock_session_identity(conn, session_id)
        assert_session_not_deleted(conn, session_id)
        assert_session_identity_available(conn, session_id)
        _lock_owned_session(
            conn,
            session_id,
            owner_user_id=owner_user_id,
            owner_username=owner_username,
        )

        barrier_enabled = relation_exists(conn, "session_deletion_barriers")
        if not barrier_enabled:
            # 没有新账本表时保持旧模式语义，由数据库原有外键决定是否可删除。
            conn.execute("DELETE FROM chat_sessions WHERE id=%s", (session_id,))
            return {
                "session_id": session_id,
                "cancelled_run_ids": [],
                "rejected_request_ids": [],
                "unknown_request_ids": [],
            }

        if barrier_enabled:
            conn.execute(
                "INSERT INTO session_deletion_barriers(session_id, deleted_at) "
                "VALUES (%s, clock_timestamp()) ON CONFLICT (session_id) DO NOTHING",
                (session_id,),
            )

        cancelled_run_ids: list[str] = []
        rejected_request_ids: list[str] = []
        unknown_request_ids: list[str] = []
        if relation_exists(conn, "report_runs"):
            runs = conn.execute(
                "SELECT run_id, state, state_version, last_event_seq, document "
                "FROM report_runs WHERE session_id=%s ORDER BY run_id FOR UPDATE",
                (session_id,),
            ).fetchall()
            db_now = conn.execute(
                "SELECT clock_timestamp() AS db_now",
            ).fetchone()["db_now"]
            for run in runs:
                cancelled = _cancel_run(conn, run, db_now=db_now)
                if cancelled is not None:
                    cancelled_run_ids.append(cancelled)
                attempt_result = _close_attempts(conn, run["run_id"])
                rejected_request_ids.extend(attempt_result["rejected_request_ids"])
                unknown_request_ids.extend(attempt_result["unknown_request_ids"])

        if relation_exists(conn, "report_session_evidence"):
            conn.execute(
                "DELETE FROM report_session_evidence WHERE session_id=%s",
                (session_id,),
            )
        conn.execute("DELETE FROM chat_sessions WHERE id=%s", (session_id,))
        return {
            "session_id": session_id,
            "cancelled_run_ids": cancelled_run_ids,
            "rejected_request_ids": rejected_request_ids,
            "unknown_request_ids": unknown_request_ids,
        }
