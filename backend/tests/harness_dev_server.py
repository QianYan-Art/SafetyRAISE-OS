"""仅供本机浏览器工程验收；不得作为生产启动入口。"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import uvicorn
from fastapi import HTTPException, Request
from psycopg.types.json import Jsonb

from app.api import deps
from app.api.routes_report_runs import get_report_run_service
from app.core.security import create_access_token
from app.core.settings import Settings
from app.main import app
from app.report_harness.release_registry import ReleaseBinding, binding_status
from app.report_harness.authorization import AuthorizationCatalog, EndpointDescription
from app.report_harness.contracts import canonical_digest
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.store import RunStore
from app.report_harness.test_database import migrate_test_database, validate_test_dsn
from app.schemas.report_run import CreateRunRequest
from app.services.database_service import DatabaseService
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles, dependencies


class SlowSyntheticRoles(SyntheticRoles):
    async def generate(self, context):
        await asyncio.sleep(1)
        return await super().generate(context)

    async def review(self, context):
        await asyncio.sleep(1)
        return await super().review(context)


class HistoricalRegistry:
    def __init__(self):
        self.binding = ReleaseBinding(
            code_digest="a" * 64, policy_digest="b" * 64, model_endpoint_digest="c" * 64,
            knowledge_manifest_digest="d" * 64, evaluation_evidence_digest="e" * 64,
            approved_by="只读合成历史fixture", approved_at="2026-09-17T00:00:00Z",
        )
        self.entries = [self.binding]

    def status(self, binding):
        return binding_status(self.entries, binding)

    def binding_for(self, _digests):
        return None


def make_settings(dsn: str, directory: Path, username: str) -> Settings:
    model = {"provider": "openai_compatible", "model": "synthetic",
             "base_url": "http://127.0.0.1:9"}
    return Settings.model_validate({
        "app": {"output_dir": str(directory / "outputs"),
                "chat_sessions_dir": str(directory / "sessions")},
        "database": {"dsn": dsn},
        "auth": {"jwt_secret": secrets.token_hex(32), "bootstrap_admin_username": username,
                 "bootstrap_admin_password": secrets.token_hex(24)},
        "input": {},
        "models": {"expert_local": model, "report_external": model, "accident_vision": model},
        "prompts": {"guidance_prompt_path": "backend/config/report_harness/generator.md",
                    "report_prompt_template": "backend/config/report_harness/generator.md"},
        "input_generation": {"workspace_dir": str(directory / "uploads"),
                             "backup_dir": str(directory / "backups"),
                             "generated_input_path": str(directory / "input.json")},
        "retrieval": {}, "workflow": {},
    })


def prepare_tables(dsn: str):
    # 只补专用测试库的旧表契约，不加载业务 bootstrap 配置。
    with psycopg.connect(dsn) as conn:
        conn.execute("""
            CREATE EXTENSION IF NOT EXISTS pgcrypto;
            CREATE TABLE IF NOT EXISTS users (id uuid PRIMARY KEY, username text UNIQUE);
            ALTER TABLE users ALTER COLUMN id SET DEFAULT gen_random_uuid();
            ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name text;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS role text DEFAULT 'user';
            ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active boolean DEFAULT true;
            ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
            ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id text PRIMARY KEY, owner_user_id uuid REFERENCES users(id), owner_username text
            );
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS title text DEFAULT '合成工程案例';
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS created_at bigint DEFAULT 0;
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS updated_at bigint DEFAULT 0;
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS sort_order integer;
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS source_type text;
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS source_name text;
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS messages jsonb DEFAULT '[]';
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS draft_json text DEFAULT '';
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS draft_meta jsonb;
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS report_result jsonb;
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS session_state text DEFAULT 'draft';
        """)
    migrate_test_database(dsn)


def cleanup(dsn: str, owner: str, session: str):
    with psycopg.connect(dsn) as conn:
        conn.execute("DELETE FROM report_run_events WHERE run_id IN "
                     "(SELECT run_id FROM report_runs WHERE owner_user_id=%s)", (owner,))
        conn.execute("DELETE FROM report_run_requests WHERE run_id IN "
                     "(SELECT run_id FROM report_runs WHERE owner_user_id=%s)", (owner,))
        conn.execute("DELETE FROM report_runs WHERE owner_user_id=%s", (owner,))
        conn.execute("DELETE FROM report_session_evidence WHERE owner_user_id=%s", (owner,))
        conn.execute("DELETE FROM chat_sessions WHERE owner_user_id=%s", (owner,))
        conn.execute("DELETE FROM session_deletion_barriers WHERE session_id=%s", (session,))
        conn.execute("DELETE FROM user_capability_configs WHERE user_id=%s", (owner,))
        conn.execute("DELETE FROM users WHERE id=%s", (owner,))


def close_database(database):
    if database._pool is not None:
        database._pool.close()


def main():
    dsn = validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    port = int(os.environ.get("HARNESS_TEST_API_PORT", "18081"))
    if not 1024 <= port <= 65535:
        raise ValueError("测试监听端口不合法")
    owner, session = str(uuid4()), "harness-browser-" + uuid4().hex
    username, control_token = "e2e-" + uuid4().hex[:12], secrets.token_hex(24)
    original_connect = socket.socket.connect

    def local_connect(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1", "localhost"}:
            raise RuntimeError("浏览器工程入口禁止非回环外联")
        return original_connect(sock, address)

    with ExitStack() as resources, TemporaryDirectory(prefix="safetyraise-browser-") as temporary:
        resources.callback(setattr, socket.socket, "connect", original_connect)
        socket.socket.connect = local_connect
        settings = make_settings(dsn, Path(temporary), username)
        prepare_tables(dsn)
        resources.callback(cleanup, dsn, owner, session)
        database = DatabaseService(settings)
        resources.callback(close_database, database)
        store = RunStore(database.connection)
        registry = HistoricalRegistry()
        run_dependencies = replace(
            dependencies(SlowSyntheticRoles()), roles_factory=SlowSyntheticRoles,
            release_registry=registry, force_engineering_exports=True,
        )
        integrated_ui = os.environ.get("HARNESS_INTEGRATED_UI_TEST") == "1"
        if integrated_ui:
            catalog = AuthorizationCatalog([
                EndpointDescription(role=role, label="本机合成角色",
                                    base_url="http://127.0.0.1:9",
                                    model="synthetic", version="v1")
                for role in ("generator", "reviewer")
            ], [], frozenset({canonical_digest([])}))
            run_dependencies = replace(
                run_dependencies, authorization_catalog=catalog,
                endpoint_profile_digest=catalog.endpoint_digest,
                knowledge_manifest_digest=catalog.knowledge_digest,
            )
        service = ReportRunService(store, run_dependencies)
        with database.connection() as conn:
            conn.execute(
                "INSERT INTO users(id,username,password_hash,display_name,role,is_active,"
                "created_at,updated_at) VALUES (%s,%s,'synthetic-unused','工程验证账号',"
                "'user',true,now(),now())", (owner, username),
            )
            # 普通用户未配置视觉与报告模型时工作区会强制打开模型配置；
            # 合成账号预置回环地址的占位配置，浏览器场景直接进入报告流程。
            for capability in ("vision", "report"):
                conn.execute(
                    "INSERT INTO user_capability_configs(user_id,capability,base_url,api_key,"
                    "model_name) VALUES (%s,%s,'http://127.0.0.1:9/v1','synthetic-unused',"
                    "'synthetic')", (owner, capability),
                )
            conn.execute(
                "INSERT INTO chat_sessions(id,owner_user_id,owner_username,title,created_at,"
                "updated_at,draft_json,messages,session_state) VALUES (%s,%s,%s,%s,%s,%s,%s,"
                "'[]','draft')",
                (session, owner, username, "合成事故 · 工程验证", int(time.time() * 1000),
                 int(time.time() * 1000), json.dumps({"事故描述": "合成路口案例，无真实人员或证据。"},
                                                  ensure_ascii=False)),
            )
        original_settings = deps.get_settings
        app.dependency_overrides[original_settings] = lambda: settings
        deps.get_settings = lambda: settings
        app.dependency_overrides[deps.get_database_service] = lambda: database
        app.dependency_overrides[get_report_run_service] = lambda: service
        token = create_access_token(auth_settings=settings.auth, user_id=owner,
                                    username=username, role="user")
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

        def check_control(request: Request):
            if (request.client is None or request.client.host not in {"127.0.0.1", "::1"}
                    or not secrets.compare_digest(
                        request.headers.get("X-Harness-Control", ""), control_token
                    )):
                raise HTTPException(403)

        @app.get("/__harness_test__/bootstrap")
        def bootstrap(request: Request):
            if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
                raise HTTPException(403)
            if integrated_ui:
                # 只打开原页面接入分支；报告服务仍是禁止外联的合成测试依赖。
                app.state.report_harness_runtime = SimpleNamespace(production_outbound_enabled=True)
            return {"token": token, "session_id": session, "control_token": control_token,
                    "quality_gate": "engineering_only"}

        @app.post("/__harness_test__/shutdown")
        def shutdown(request: Request):
            check_control(request)
            server.should_exit = True
            return {"stopping": True}

        @app.post("/__harness_test__/scenario/{name}")
        def scenario(name: str, request: Request):
            check_control(request)
            if name == "revoke":
                registry.entries = []
                return {"revoked": True}
            if name == "unknown":
                from app.report_harness.evidence_store import EvidenceStore
                revision = EvidenceStore(store).get(owner, session)["revision"]
                record = service.create(owner, CreateRunRequest(
                    request_id=uuid4(), session_id=session, accident_data={"事故描述": "合成未知请求场景"},
                    evidence_revision=revision,
                ))
                run_id = record["run_id"]
                lease = service.claim(owner, run_id, record["state_version"])
                ledger = RequestLedger(store)
                ledger.bind_runtime_profile(owner, run_id, lease, {
                    "generation_reserve_tokens": 100, "review_reserve_tokens": 100,
                })
                attempt = ledger.reserve(
                    owner, run_id, lease, role="generator",
                    endpoint_digest=run_dependencies.endpoint_profile_digest,
                    request_digest="f" * 64, reserved_tokens=100, review_reserve_tokens=100,
                )
                ledger.dispatch(owner, run_id, lease, attempt["request_id"], attempt["attempt_id"])
                ledger.mark_unknown(owner, run_id, lease, attempt["request_id"], attempt["attempt_id"])
                store.transition(owner, run_id, lease, "suspended",
                                 {"terminal_reason": "usage_unknown"}, "error", {"reason": "usage_unknown"})
                return {"run_id": run_id, "fixture_scope": "仅UI状态场景，不冒充真实进程崩溃"}
            if name == "historical":
                run_id = str(uuid4())
                record = {
                    "run_id": run_id, "session_id": session, "state_version": 1,
                    "snapshot_digest": "f" * 64, "candidate_version": 1, "review_status": "passed",
                    "terminal_reason": None, "budget": {}, "last_event_seq": 0,
                    "quality_gate": "quality_validated", "execution_profile": "outbound",
                    "formal_export_eligible": True, "release_binding_status": "approved",
                    "release_binding": registry.binding.model_dump(mode="json"),
                    "report": {"report_markdown": "# 只读合成历史样本\n\n没有真实事故或真实批准。",
                               "sections": [], "citations": [], "meta": {"fixture": True}},
                }
                with database.connection() as conn:
                    conn.execute(
                        "INSERT INTO report_runs(run_id,owner_user_id,session_id,request_id,"
                        "request_digest,state,state_version,document) "
                        "VALUES(%s,%s,%s,%s,%s,'published',1,%s)",
                        (run_id, owner, session, uuid4(), "0" * 64, Jsonb(record)),
                    )
                return {"run_id": run_id, "fixture_scope": "只读历史批准fixture，下载仍带工程标记"}
            raise HTTPException(404)

        server.run()


if __name__ == "__main__":
    main()
