"""07 故障恢复测试 worker。

该进程只接收测试父进程显式传入的专用 PostgreSQL DSN 和回环 HTTP 地址。
它不加载业务 settings、.env 或默认端点；所有故障注入都通过测试侧包装器完成。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.execution import ReportExecutionDependencies
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.test_database import validate_test_dsn
from app.report_harness.transport import (
    BudgetedTransport,
    HTTPAttemptClient,
    RequestBound,
)
from app.report_harness.transport_roles import RoleModel, TransportRoles
from app.report_harness.store import RunStore
from app.schemas.report_run import BudgetPolicy
from app.services.report_run_service import ReportRunService

try:
    from tests.harness_fixtures import SyntheticRoles
except ModuleNotFoundError:
    from harness_fixtures import SyntheticRoles


PROOF_DIGEST = canonical_digest({"protocol": "fixed-synthetic-http-usage"})
ENDPOINT_PROFILE_DIGEST = canonical_digest({"profile": "recovery-crash-worker-v1"})
POLICY_DIGEST = canonical_digest({"policy": "recovery-crash-worker-v1"})
KNOWLEDGE_MANIFEST_DIGEST = canonical_digest({"knowledge": []})
WORKER_PATH = Path(__file__).resolve()


def _connection_factory(dsn: str):
    @contextmanager
    def connection():
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            yield conn

    return connection


def _validate_endpoint(address: str) -> str:
    parsed = urlsplit(address)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("故障测试端点必须是无凭据的 127.0.0.1 HTTP 地址。")
    return address


def _validate_dsn(value: str) -> str:
    dsn = validate_test_dsn(value)
    params = conninfo_to_dict(dsn)
    if params.get("host") != "127.0.0.1":
        raise ValueError("故障测试 worker 只接受 127.0.0.1 DSN。")
    if params.get("hostaddr", "127.0.0.1") != "127.0.0.1":
        raise ValueError("故障测试 worker 只接受 127.0.0.1 hostaddr。")
    return dsn


class CrashGate:
    """父进程看到 marker 后 kill；未命中的 worker 不阻塞。"""

    def __init__(self, marker: str | None, target: str | None):
        self.path = Path(marker) if marker else None
        self.target = target
        self._hit = False

    def hit(self, name: str, **details: Any) -> None:
        if self._hit or self.path is None or self.target != name:
            return
        self._hit = True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"marker": name, "pid": os.getpid(), **details}
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        while True:
            time.sleep(0.2)


class CrashStore:
    """只在测试代理层观察 transition，底层 RunStore 仍负责真实事务。"""

    def __init__(self, store: RunStore, gate: CrashGate):
        self._store = store
        self._gate = gate
        self._published_commit_pending = False

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    @contextmanager
    def locked(self, *args, **kwargs):
        with self._store.locked(*args, **kwargs) as pair:
            yield pair
        if self._published_commit_pending:
            self._published_commit_pending = False
            self._gate.hit("published_checkpoint")

    def transition(
        self,
        owner: str,
        run_id: str,
        token: int,
        state: str,
        patch: dict,
        event_type: str = "stage",
        data: dict | None = None,
    ) -> dict:
        result = self._store.transition(
            owner, run_id, token, state, patch, event_type, data,
        )
        event_data = data or {}
        if event_type == "tool" and event_data.get("status") == "completed":
            self._gate.hit("tool_checkpoint")
        if event_data.get("step") == "candidate":
            self._gate.hit("candidate_checkpoint")
        if event_data.get("step") == "review_validated":
            self._gate.hit("review_checkpoint")
        return result

    def save(
        self,
        conn,
        row: dict,
        state: str,
        document: dict,
        event_type: str,
        data: dict,
    ) -> dict:
        result = self._store.save(conn, row, state, document, event_type, data)
        if state == "published" or event_type == "final":
            self._published_commit_pending = True
        return result


class CrashHTTPClient:
    """在真实 HTTP 响应返回后、账本 settle 前停住。"""

    def __init__(self, client: HTTPAttemptClient, gate: CrashGate):
        self._client = client
        self._gate = gate

    @property
    def registered_roles(self) -> tuple[str, ...]:
        return self._client.registered_roles

    async def attempt(self, role: str, payload: dict, timeout: float) -> dict:
        result = await self._client.attempt(role, payload, timeout)
        self._gate.hit("http_response_before_settle", role=role)
        return result

    async def close(self) -> None:
        await self._client.close()


class CrashLedger(RequestLedger):
    """在账本操作成功提交后注入 marker，不改生产账本。"""

    def __init__(self, store, gate: CrashGate):
        super().__init__(store)
        self._gate = gate

    def dispatch(self, owner, run_id, token, request_id, attempt_id):
        result = super().dispatch(owner, run_id, token, request_id, attempt_id)
        self._gate.hit("dispatch_checkpoint")
        return result

    def settle(self, owner, run_id, token, request_id, attempt_id, *,
               actual_tokens: int | None, result: dict):
        settled = super().settle(
            owner, run_id, token, request_id, attempt_id,
            actual_tokens=actual_tokens, result=result,
        )
        self._gate.hit("settle_before_journal")
        return settled


def _policy_from_record(record: dict) -> BudgetPolicy:
    value = record.get("budget_policy")
    if not isinstance(value, dict):
        raise HarnessError("budget_policy_missing")
    return BudgetPolicy.model_validate(deepcopy(value))


def _dependencies(
    store: CrashStore,
    owner: str,
    run_id: str,
    endpoint: str,
    gate: CrashGate,
) -> ReportExecutionDependencies:
    record = store.get(owner, run_id)
    policy = _policy_from_record(record)
    endpoint_digest = record.get("endpoint_profile_digest")
    if endpoint_digest != ENDPOINT_PROFILE_DIGEST:
        raise HarnessError("endpoint_profile_unavailable")
    manifest = record["snapshot"]["knowledge_manifest_digest"]
    if manifest != KNOWLEDGE_MANIFEST_DIGEST:
        raise HarnessError("knowledge_manifest_unavailable")

    def runtime_factory(runtime_store, runtime_owner, runtime_run_id, runtime_token):
        ledger = CrashLedger(runtime_store, gate)
        raw_client = HTTPAttemptClient({
            "generator": endpoint,
            "reviewer": endpoint,
        })
        client = CrashHTTPClient(raw_client, gate)
        transport = BudgetedTransport(
            ledger,
            client,
            owner=runtime_owner,
            run_id=runtime_run_id,
            token=runtime_token,
            endpoint_digest=ENDPOINT_PROFILE_DIGEST,
            output_limit=20,
            review_reserve_tokens=200,
            generation_reserve_tokens=200,
            authorize=lambda: runtime_store.assert_active(
                runtime_owner, runtime_run_id, runtime_token,
            ),
            remaining_seconds=lambda: max(
                1.0,
                30.0 - float(runtime_store.get(
                    runtime_owner, runtime_run_id,
                ).get("active_seconds", 0) or 0),
            ),
            bound_provider=lambda _role, _payload: RequestBound(
                total_tokens=200,
                output_tokens=20,
                proof_digest=PROOF_DIGEST,
            ),
            verified_proofs=frozenset({PROOF_DIGEST}),
        )
        return TransportRoles(
            transport,
            {
                "generator": RoleModel("synthetic-generator"),
                "reviewer": RoleModel("synthetic-reviewer"),
            },
        )

    return ReportExecutionDependencies(
        roles_factory=lambda: SyntheticRoles(),
        runtime_roles_factory=runtime_factory,
        execution_profile="synthetic_test",
        endpoint_profile_digest=ENDPOINT_PROFILE_DIGEST,
        policy_digest=record.get("policy_digest", POLICY_DIGEST),
        knowledge_manifest_digest=KNOWLEDGE_MANIFEST_DIGEST,
        budget_policy=policy,
    )


def _execute(args: argparse.Namespace) -> dict:
    dsn = _validate_dsn(args.dsn)
    endpoint = _validate_endpoint(args.endpoint)
    connection = _connection_factory(dsn)
    base_store = RunStore(connection)
    gate = CrashGate(args.marker, args.crash_at)
    store = CrashStore(base_store, gate)

    if args.action == "replay":
        return base_store.get(args.owner, args.run_id)

    dependencies = _dependencies(store, args.owner, args.run_id, endpoint, gate)
    service = ReportRunService(store, dependencies)
    if args.action == "execute":
        return asyncio.run(service.execute(args.owner, args.run_id, args.expected_version))
    if args.action == "resume":
        token = service.resume_claim(
            args.owner,
            args.run_id,
            args.expected_version,
            retry_unknown_requests=args.retry_unknown_requests,
        )
        result = asyncio.run(service.execute_claimed(args.owner, args.run_id, token))
        return {"token": token, "result": result}
    raise ValueError("未知 worker action。")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="报告恢复故障测试 worker")
    parser.add_argument("--action", choices=("execute", "resume", "replay"), required=True)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--expected-version", type=int, default=0)
    parser.add_argument("--retry-unknown-requests", action="store_true")
    parser.add_argument("--marker")
    parser.add_argument(
        "--crash-at",
        choices=(
            "tool_checkpoint",
            "http_response_before_settle",
            "settle_before_journal",
            "candidate_checkpoint",
            "review_checkpoint",
            "published_checkpoint",
            "dispatch_checkpoint",
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _execute(args)
    except Exception as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
