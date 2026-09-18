"""经明确授权的本机开发样本执行器；不注册生产入口，不读取验收参照。"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing, contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import time
from uuid import uuid4
import xml.etree.ElementTree as ET

import httpx
import psycopg
from psycopg.rows import dict_row

from app.report_harness.authorization import (
    AuthorizationCatalog, AuthorizationRequest, EndpointDescription,
)
from app.report_harness.contracts import canonical_digest
from app.report_harness.execution import ReportExecutionDependencies
from app.report_harness.errors import HarnessError
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.store import RunStore
from app.report_harness.test_database import validate_test_dsn
from app.report_harness.transport import BudgetedTransport, RequestBound
from app.report_harness.transport_roles import RoleModel
from evals.report_harness.quoted_roles import QuotedTransportRoles
from app.schemas.report_run import BudgetPolicy, CreateRunRequest
from app.services.report_run_service import ReportRunService
from evals.report_harness.development_provider import (
    DevelopmentClient, ENDPOINT, MODEL, OUTPUT_LIMIT, TOKEN_BOUND,
    REQUEST_CNY_UPPER, USD_TO_CNY_UPPER, GENERATION_SETTINGS, validate_metadata,
)


def write_json(path: Path, data: dict):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False, default=str)


def public_preflight() -> dict:
    with httpx.Client(timeout=25, follow_redirects=False, trust_env=False) as client:
        response = client.get("https://openrouter.ai/api/v1/models/tencent/hy4-preview/endpoints")
        response.raise_for_status()
        metadata = response.json()["data"]
        endpoint = validate_metadata(metadata)
        response = client.get("https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml")
        response.raise_for_status()
        root = ET.fromstring(response.content)
    rates = {}
    published = None
    for element in root.iter():
        if "time" in element.attrib:
            published = date.fromisoformat(element.attrib["time"])
        if element.attrib.get("currency") in {"USD", "CNY"}:
            rates[element.attrib["currency"]] = Decimal(element.attrib["rate"])
    today = datetime.now(timezone.utc).date()
    if published is None or not 0 <= (today - published).days <= 4:
        raise ValueError("汇率依据过期或日期异常。")
    ratio = rates["CNY"] / rates["USD"]
    if not ratio.is_finite() or not 0 < ratio < USD_TO_CNY_UPPER:
        raise ValueError("保守记账汇率不能覆盖参考汇率。")
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint, "ecb_date": str(published),
        "ecb_cny_per_usd": str(ratio),
        "accounting_cny_per_usd_upper": str(USD_TO_CNY_UPPER),
        "request_cny_upper": str(REQUEST_CNY_UPPER),
        "price_limit_usd_per_million": {"prompt": 1, "completion": 3},
        "currency_scope": "模型账户扣费；不发起充值或另行付费服务",
    }


def load_input(path: Path, expected_sha256: str) -> dict:
    raw = path.read_bytes()
    if len(raw) > 64 * 1024 or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("输入摘要不匹配或过大。")
    result = json.loads(raw)
    if not isinstance(result, dict) or not result or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in result.items()
    ):
        raise ValueError("开发输入必须为非空文字字段对象。")
    return result


def execution_proof(version: str) -> dict:
    return {
        "model": MODEL, "endpoint": ENDPOINT, "version": version,
        "token_bound": TOKEN_BOUND, "output_limit": OUTPUT_LIMIT,
        "price_caps": {"prompt": "0.000001", "completion": "0.000003",
                       "input_cache_read": "0.000001"},
        "accounting_fx": str(USD_TO_CNY_UPPER), "request_upper": str(REQUEST_CNY_UPPER),
        "money_limit_cny": "100", "money_enforcement": "persistent_physical_attempt_guard",
        "provider": "tencent", "provider_slug": "tencent/fp8", "fallbacks": False,
    }


def check_unknown_costs(experiment: Path, acknowledged: list[int]) -> None:
    if any(type(item) is not int or item < 1 for item in acknowledged):
        raise ValueError("未知请求确认编号无效。")
    ledger = experiment / "money.sqlite3"
    if not ledger.exists():
        return
    with closing(sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True)) as connection:
        pending = {row[0] for row in connection.execute(
            "SELECT attempt_id FROM money_guard_attempts "
            "WHERE experiment_id=? AND state IN ('reserved','unknown')",
            ("report-evidence-v1-Q-CNY100",),
        )}
    if pending - set(acknowledged):
        raise HarnessError("unknown_cost_ack_required")


async def run(args):
    if not args.confirm_outbound:
        raise ValueError("必须显式确认本次开发外发。")
    experiment = Path(args.experiment_dir).resolve()
    repo = Path(__file__).resolve().parents[3]
    if experiment == repo or repo in experiment.parents:
        raise ValueError("私有试验产物不能进入源码仓。")
    check_unknown_costs(experiment, args.acknowledge_unknown_attempts)
    data = load_input(Path(args.input), args.input_sha256)
    dsn = validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise ValueError("缺少指定API密钥，不回退其他提供者。")
    preflight = public_preflight()
    experiment.mkdir(parents=True, exist_ok=True)
    attempt_dir = experiment / ("attempt-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
    attempt_dir.mkdir()
    write_json(attempt_dir / "preflight.json", preflight)
    write_json(attempt_dir / "input.json", data)
    source_paths = sorted({
        *repo.glob("backend/app/report_harness/**/*.py"),
        *repo.glob("backend/evals/report_harness/*.py"),
        *repo.glob("backend/config/report_harness/*.md"),
        repo / "backend/app/services/report_run_service.py",
        repo / "backend/app/schemas/report_run.py",
    })
    write_json(attempt_dir / "source.json", {
        "commit": subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True,
        ).strip(),
        "working_tree": subprocess.check_output(
            ["git", "-C", str(repo), "status", "--porcelain"], text=True,
        ).splitlines(),
        "sha256": {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in source_paths},
        "generation_settings": GENERATION_SETTINGS,
    })

    @contextmanager
    def connection():
        with psycopg.connect(dsn, row_factory=dict_row) as conn:
            yield conn

    store = RunStore(connection)
    store.check_schema()
    owner = str(uuid4())
    session = "q-development-" + uuid4().hex
    with connection() as conn:
        conn.execute("INSERT INTO users(id,username) VALUES (%s,%s)", (owner, session))
        conn.execute("INSERT INTO chat_sessions(id,owner_user_id) VALUES (%s,%s)", (session, owner))
    knowledge_digest = canonical_digest([])
    version = preflight["endpoint"]["name"]
    catalog = AuthorizationCatalog([
        EndpointDescription(role=role, label="获准开发试跑", base_url=ENDPOINT,
                            model=MODEL, version=version)
        for role in ("generator", "reviewer")
    ], [], frozenset({knowledge_digest}))
    proof = execution_proof(version)
    proof_digest = canonical_digest(proof)
    policy = BudgetPolicy(
        max_physical_requests=8, max_tool_calls=12, max_revision_rounds=2,
        max_retrieval_requests=0, max_active_seconds=900,
        max_total_tokens=TOKEN_BOUND * 10, max_output_tokens_per_request=OUTPUT_LIMIT,
        # 请求账本只核token；货币限额由下层持久MoneyGuard独立执行。
        max_money=None,
    )
    service = None
    clients = []

    def roles_factory(active_store, active_owner, run_id, token):
        # 延迟导入只供该显式入口使用，生产依赖不会加载试验预算器。
        from evals.report_harness.money_guard import MoneyGuard

        client = DevelopmentClient(key)
        clients.append(client)
        guarded = MoneyGuard(
            client=client, path=experiment / "money.sqlite3",
            experiment_id="report-evidence-v1-Q-CNY100",
            profile=proof, cost_upper_cny=REQUEST_CNY_UPPER,
            usd_to_cny_upper=USD_TO_CNY_UPPER,
        )
        started = time.monotonic()

        def authorize():
            check_unknown_costs(experiment, args.acknowledge_unknown_attempts)
            active_store.assert_active(active_owner, run_id, token)
            service._validate_profile(active_store.get(active_owner, run_id), active_owner)

        transport = BudgetedTransport(
            RequestLedger(active_store), guarded, owner=active_owner, run_id=run_id, token=token,
            endpoint_digest=catalog.endpoint_digest, output_limit=OUTPUT_LIMIT,
            review_reserve_tokens=TOKEN_BOUND, generation_reserve_tokens=TOKEN_BOUND,
            authorize=authorize, remaining_seconds=lambda: 900 - (time.monotonic() - started),
            bound_provider=lambda role, payload: RequestBound(TOKEN_BOUND, OUTPUT_LIMIT, proof_digest),
            verified_proofs=frozenset({proof_digest}),
        )
        return QuotedTransportRoles(transport, {
            role: RoleModel(MODEL, json_object_mode=True) for role in ("generator", "reviewer")
        })

    dependencies = ReportExecutionDependencies(
        roles_factory=lambda: None, runtime_roles_factory=roles_factory,
        execution_profile="outbound", endpoint_profile_digest=catalog.endpoint_digest,
        policy_digest=canonical_digest({
            "budget": policy.model_dump(mode="json"), "proof": proof,
            "generation_settings": GENERATION_SETTINGS,
            "wire_protocol": "unique-quote-spans-v1",
        }),
        knowledge_manifest_digest=knowledge_digest, authorization_catalog=catalog,
        budget_policy=policy, max_active_seconds=900, force_engineering_exports=True,
        development_outbound_enabled=True,
    )
    service = ReportRunService(store, dependencies)
    record = service.create(owner, CreateRunRequest(
        request_id=uuid4(), session_id=session, evidence_revision=0, accident_data=data,
    ))
    run_id = record["run_id"]
    write_json(attempt_dir / "identity.json", {
        "owner": owner, "session": session, "run_id": run_id, "input_sha256": args.input_sha256,
        "scope": "开发样本，不是完整Q验收；无外部知识，不作法律责任判定",
        "proof": proof, "proof_digest": proof_digest,
        "generation_settings": GENERATION_SETTINGS,
        "wire_protocol": "unique-quote-spans-v1",
        "acknowledged_unknown_attempts": args.acknowledge_unknown_attempts,
    })
    service.authorize(owner, run_id, AuthorizationRequest(
        snapshot_digest=record["snapshot_digest"], endpoint_profile_digest=catalog.endpoint_digest,
        approved_knowledge_manifest_digest=knowledge_digest, confirmed=True,
    ))
    record = service.get(owner, run_id)
    failure = None
    try:
        await service.execute(owner, run_id, record["state_version"])
    except Exception as exc:
        # 私有诊断只记录异常类型，不输出潜在携带凭据或正文的异常消息。
        failure = type(exc).__name__
    finally:
        for client in clients:
            await client.close()
    record = store.get(owner, run_id)
    write_json(attempt_dir / "result.json", {
        "failure_type": failure, "record": record,
        "last_http_statuses": [client.last_http_status for client in clients],
        "transport_error_types": [client.last_error_type for client in clients],
    })
    write_json(attempt_dir / "events.json", store.events(owner, run_id, 0, 500))
    candidate = record.get("candidate")
    if candidate:
        marker = "工程验证样本（真实模型开发报告），待老师独立验收；不是正式责任认定，也不代表Q通过。"
        if record["state"] != "published":
            marker += "\n\n系统检查未通过，不可按最终报告使用。"
        (attempt_dir / "report.md").write_text(
            marker + "\n\n" + candidate["report_markdown"], encoding="utf-8",
        )
    print(json.dumps({
        "output": str(attempt_dir), "state": record["state"],
        "reason": record.get("terminal_reason"), "candidate_available": bool(candidate),
        "failure_type": failure, "budget": service.get(owner, run_id)["budget"],
    }, ensure_ascii=True, default=str))
    return 0 if record["state"] == "published" else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--input-sha256", required=True)
    parser.add_argument("--experiment-dir", required=True)
    parser.add_argument("--confirm-outbound", action="store_true")
    parser.add_argument("--acknowledge-unknown-attempts", nargs="+", type=int, default=[])
    try:
        status = asyncio.run(run(parser.parse_args()))
    except Exception as exc:
        print(json.dumps({
            "status": "failed_before_delivery", "error_type": type(exc).__name__,
            "error_code": exc.code if isinstance(exc, HarnessError) else None,
        }))
        raise SystemExit(1) from None
    raise SystemExit(status)


if __name__ == "__main__":
    main()
