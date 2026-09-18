"""真实 API、PostgreSQL、回环模型 HTTP 与 SQLite 货币账本；响应为合成数据。"""

import asyncio
from contextlib import contextmanager
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import threading
from uuid import uuid4

from app.report_harness.authorization import AuthorizationCatalog, EndpointDescription, KnowledgeCollection
from app.report_harness.business_bootstrap import billing_endpoint_digest
from app.report_harness.contracts import canonical_digest
from app.report_harness.money_guard import (
    ALLOWED_MODEL, MoneyGuardHTTPAttemptClient, RoleBillingContract,
    VersionedMoneyGuardHTTPAttemptClient, read_billing_contract,
)
from app.report_harness.runtime_factory import build_business_dependencies
from app.schemas.report_run import BudgetPolicy
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles
from tests.test_run_recovery import _api_client
from tests.unit.test_business_runtime_integration import (
    _capacity, _LegacyClient, _prompts, _RegistrationClient, _SyntheticRetriever,
)


@contextmanager
def model_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            role = self.path.removeprefix("/")
            calls.append((role, payload))
            if role == "embedding":
                response = {
                    "data": [{"index": 0, "embedding": [0.1, 0.2]}],
                    "usage": {"total_tokens": 4, "cost": "0.01"},
                }
            else:
                if role == "expert":
                    content = '<think>不应入账的合成思考</think>{"建议":"按合成事实审查"}'
                else:
                    context = json.loads(payload["messages"][-1]["content"])
                    roles = SyntheticRoles()
                    result = asyncio.run(
                        roles.generate(context) if role == "generator" else roles.review(context),
                    )
                    if role == "generator":
                        for claim in result["claims"]:
                            span = claim.pop("text_span")
                            claim["quote"] = result["report_markdown"][span["start"]:span["end"]]
                    content = json.dumps(result, ensure_ascii=False)
                response = {
                    "choices": [{"finish_reason": "stop", "message": {"content": content}}],
                    "usage": {"total_tokens": 7, **({"cost": "0.01"} if role != "expert" else {})},
                }
            raw = json.dumps(response, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_business_api_publishes_engineering_candidate_through_all_real_transports(pg_store, tmp_path):
    store, owner, _, session = pg_store
    with model_server() as (base, calls):
        roles = ("expert", "embedding", "generator", "reviewer")
        capacities = {
            role: _capacity(
                role,
                ALLOWED_MODEL if role in {"generator", "reviewer"} else "synthetic-" + role,
                0 if role == "embedding" else 8192,
                "high" if role in {"generator", "reviewer"} else None,
            ) for role in roles
        }
        endpoints = {role: base + "/" + role for role in roles}
        collection = KnowledgeCollection(
            collection_id="synthetic-http", label="合成规则", version="1",
            content_digest=canonical_digest("合成规则"),
        )
        manifest = canonical_digest([collection.model_dump(mode="json")])
        text = "合成路口碰撞资料，仅验证工程流程。"
        chunks = ({
            "id": "synthetic-rule", "document_id": "synthetic-doc", "version": "1",
            "text": text, "digest": canonical_digest(text), "manifest_digest": manifest,
        },)
        catalog = AuthorizationCatalog([
            EndpointDescription(
                role=role, label=role, base_url=endpoints[role],
                model=capacities[role].model, version=capacities[role].proof_digest,
            ) for role in roles
        ], [collection], frozenset({manifest}))
        path = tmp_path / "synthetic-money.sqlite3"

        async def register():
            seed = MoneyGuardHTTPAttemptClient(
                _LegacyClient(), path, "report-evidence-v1-Q-CNY100",
                Decimal("1"), Decimal("7"), model=ALLOWED_MODEL,
                endpoint_summary={"synthetic": "http-pg"},
            )
            await seed.close()
            client = VersionedMoneyGuardHTTPAttemptClient(
                _RegistrationClient(), path, "report-evidence-v1-Q-CNY100",
            )
            try:
                for role in roles:
                    current = read_billing_contract(path, "report-evidence-v1-Q-CNY100")
                    client.register_contract(RoleBillingContract(
                        role=role, model=capacities[role].model,
                        endpoint_digest=billing_endpoint_digest(role, endpoints[role], capacities[role]),
                        quote_cny=Decimal(0 if role == "expert" else 1),
                        billing_mode="local_token_free" if role == "expert" else "remote_actual",
                        usage_source="total_tokens" if role == "expert" else "cost_usd",
                    ), client.billing_contract_digest, expected_previous_role_digest=(
                        current["contracts"][role].digest if role in current["contracts"] else None
                    ))
            finally:
                await client.close()

        asyncio.run(register())
        billing = read_billing_contract(path, "report-evidence-v1-Q-CNY100")
        runtime = build_business_dependencies(
            workflow=_prompts(), capacities=capacities, endpoints=endpoints, headers={},
            catalog=catalog, knowledge_chunks=chunks,
            budget_policy=BudgetPolicy(max_physical_requests=16, max_output_tokens_per_request=8192),
            monetary_client_factory=lambda raw: VersionedMoneyGuardHTTPAttemptClient(
                raw, path, "report-evidence-v1-Q-CNY100", contracts=billing["contracts"],
            ),
            retriever_factory=lambda embedding: _SyntheticRetriever(embedding, chunks),
            validate_source=lambda: None, embedding_dimensions=2,
            billing_contract_digest=billing["billing_contract_digest"], resource_check=lambda: None,
        )
        service = ReportRunService(store, runtime)
        with _api_client(pg_store, service) as (client, headers):
            response = client.post("/api/v1/report-runs", headers=headers, json={
                "request_id": str(uuid4()), "session_id": session, "evidence_revision": 0,
                "accident_data": {"事故经过": "合成路口碰撞", "天气": "合成晴天"},
            })
            assert response.status_code == 201, response.text
            run = response.json()
            prefix = "/api/v1/report-runs/" + run["run_id"]
            denied = client.post(prefix + "/execute/stream", headers=headers,
                                 json={"expected_version": run["state_version"]})
            assert denied.status_code != 200
            assert calls == []
            preview = client.get(prefix + "/authorization-preview", headers=headers).json()
            approved = client.post(prefix + "/authorize", headers=headers, json={
                name: preview[name] for name in (
                    "snapshot_digest", "endpoint_profile_digest", "approved_knowledge_manifest_digest",
                )
            } | {"confirmed": True})
            assert approved.status_code == 200, approved.text
            response = client.post(prefix + "/execute/stream", headers=headers,
                                   json={"expected_version": approved.json()["state_version"]})
            assert response.status_code == 200, response.text
            final = client.get(prefix, headers=headers).json()
            assert final["state"] == "published", final
            assert final["quality_gate"] == "engineering_only"
            assert final["formal_export_eligible"] is False
            assert final["budget"]["physical_requests"] == 4
            assert [role for role, _ in calls] == list(roles)
        persisted = store.get(owner, run["run_id"])
        assert persisted["initial_knowledge_snippets"][0]["id"] == "synthetic-rule"
        with store.connection() as connection:
            rows = connection.execute(
                "SELECT role,result FROM report_run_requests WHERE run_id=%s",
                (run["run_id"],),
            ).fetchall()
        assert {row["role"] for row in rows} == set(roles)
        expert = next(row["result"] for row in rows if row["role"] == "expert")
        assert "<think>" not in expert["choices"][0]["message"]["content"]
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT count(*) FROM money_guard_experiments").fetchone()[0] == 1
            assert connection.execute(
                "SELECT count(*) FROM money_guard_attempts WHERE state='settled'",
            ).fetchone()[0] == 4
