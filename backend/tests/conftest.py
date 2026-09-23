from __future__ import annotations

import os
import socket
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from app.report_harness.store import RunStore
from app.report_harness.test_database import migrate_test_database, validate_test_dsn
from psycopg.rows import dict_row


@pytest.fixture(autouse=True)
def deny_external_network(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("离线测试禁止非回环网络连接。")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def pg_store():
    dsn = validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    # 独立测试库仅建立迁移所需的旧表契约，不加载业务 settings 或 .env。
    with psycopg.connect(dsn) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id uuid PRIMARY KEY,
                username text NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id text PRIMARY KEY,
                title text NOT NULL DEFAULT '',
                owner_user_id uuid REFERENCES users(id),
                owner_username text,
                created_at bigint NOT NULL DEFAULT 0,
                updated_at bigint NOT NULL DEFAULT 0,
                sort_order integer,
                source_type text,
                source_name text,
                messages jsonb NOT NULL DEFAULT '[]'::jsonb,
                draft_json text NOT NULL DEFAULT '',
                draft_meta jsonb,
                report_result jsonb,
                session_state text NOT NULL DEFAULT 'draft'
            );
        """)
    migrate_test_database(dsn)

    @contextmanager
    def connection():
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            yield conn

    store = RunStore(connection)
    owner, other = str(uuid4()), str(uuid4())
    session = "harness-" + str(uuid4())
    with connection() as conn:
        conn.execute("INSERT INTO users(id,username) VALUES (%s,%s),(%s,%s)",
                     (owner, owner, other, other))
        conn.execute("INSERT INTO chat_sessions(id,owner_user_id) VALUES (%s,%s)",
                     (session, owner))
    try:
        yield store, owner, other, session
    finally:
        with connection() as conn, conn.transaction():
            ids = [row["run_id"] for row in conn.execute(
                "SELECT run_id FROM report_runs WHERE session_id=%s", (session,)
            ).fetchall()]
            for run_id in ids:
                conn.execute("DELETE FROM report_run_feedback WHERE run_id=%s", (run_id,))
                conn.execute("DELETE FROM report_run_events WHERE run_id=%s", (run_id,))
                conn.execute("DELETE FROM report_run_requests WHERE run_id=%s", (run_id,))
                conn.execute("DELETE FROM report_runs WHERE run_id=%s", (run_id,))
            conn.execute("DELETE FROM report_session_evidence WHERE session_id=%s", (session,))
            conn.execute("DELETE FROM chat_sessions WHERE id=%s", (session,))
            conn.execute("DELETE FROM session_deletion_barriers WHERE session_id=%s", (session,))
            conn.execute("DELETE FROM users WHERE id IN (%s,%s)", (owner, other))
