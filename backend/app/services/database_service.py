from __future__ import annotations

from contextlib import contextmanager
from threading import Lock
from typing import Iterator

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.core.settings import Settings

_POOL_CACHE: dict[str, ConnectionPool] = {}
_POOL_CACHE_LOCK = Lock()
_BOOTSTRAPPED_DSNS: set[str] = set()
_BOOTSTRAP_LOCK = Lock()


class DatabaseService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._pool = self._get_or_create_pool()
        self._ensure_bootstrap()

    @contextmanager
    def connection(self) -> Iterator[Connection]:
        with self._pool.connection() as conn:
            conn.row_factory = dict_row
            yield conn

    def _get_or_create_pool(self) -> ConnectionPool:
        key = self.settings.database.dsn
        with _POOL_CACHE_LOCK:
            pool = _POOL_CACHE.get(key)
            if pool is None:
                pool = ConnectionPool(
                    conninfo=key,
                    min_size=self.settings.database.min_pool_size,
                    max_size=self.settings.database.max_pool_size,
                    kwargs={
                        "connect_timeout": self.settings.database.connect_timeout_seconds,
                    },
                    open=True,
                )
                _POOL_CACHE[key] = pool
            return pool

    def _ensure_bootstrap(self) -> None:
        dsn = self.settings.database.dsn
        with _BOOTSTRAP_LOCK:
            if dsn in _BOOTSTRAPPED_DSNS:
                return
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    create extension if not exists pgcrypto;

                    create table if not exists users (
                        id uuid primary key default gen_random_uuid(),
                        username text not null unique,
                        password_hash text not null,
                        display_name text,
                        role text not null check (role in ('admin', 'user')),
                        is_active boolean not null default true,
                        created_at timestamptz not null default now(),
                        updated_at timestamptz not null default now()
                    );

                    create table if not exists user_capability_configs (
                        user_id uuid not null references users(id) on delete cascade,
                        capability text not null check (capability in ('vision', 'embedding', 'report')),
                        base_url text,
                        api_key text,
                        model_name text,
                        params jsonb not null default '{}'::jsonb,
                        updated_at timestamptz not null default now(),
                        primary key (user_id, capability)
                    );

                    create table if not exists chat_sessions (
                        id text primary key,
                        title text not null,
                        owner_user_id uuid references users(id) on delete set null,
                        owner_username text,
                        created_at bigint not null,
                        updated_at bigint not null,
                        sort_order integer,
                        source_type text,
                        source_name text,
                        messages jsonb not null default '[]'::jsonb,
                        draft_json text not null default '',
                        draft_meta jsonb,
                        report_result jsonb,
                        session_state text not null default 'draft'
                    );

                    create index if not exists idx_chat_sessions_owner_user_id
                        on chat_sessions(owner_user_id);

                    create index if not exists idx_chat_sessions_updated_at
                        on chat_sessions(updated_at desc);

                    create table if not exists app_settings (
                        setting_key text primary key,
                        payload jsonb not null,
                        updated_at timestamptz not null default now()
                    );
                    """
                )
            conn.commit()
        with _BOOTSTRAP_LOCK:
            _BOOTSTRAPPED_DSNS.add(dsn)
