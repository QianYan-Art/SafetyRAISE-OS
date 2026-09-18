from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.report_harness.authorization import (
    AuthorizationCatalog,
    AuthorizationRequest,
    EndpointDescription,
    KnowledgeCollection,
)
from app.report_harness.business_workflow import BusinessWorkflow
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.money_guard import (
    ALLOWED_MODEL,
    MoneyGuardHTTPAttemptClient,
    RoleBillingContract,
    VersionedMoneyGuardHTTPAttemptClient,
    read_billing_contract,
)
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.runtime_factory import build_business_dependencies
from app.report_harness.runtime_profiles import ModelCapacity
from app.schemas.report_run import BudgetPolicy, CandidateReport, CreateRunRequest, ReviewResult
from app.services.report_run_service import ReportRunService
from app.workflow.prompt_rendering import (
    GUIDANCE_ACCIDENT_PLACEHOLDER,
    REPORT_ACCIDENT_ANCHOR_PLACEHOLDER,
    REPORT_ACCIDENT_PLACEHOLDER,
    REPORT_ADDITIONAL_SNIPPETS_PLACEHOLDER,
    REPORT_AGENTIC_HISTORY_PLACEHOLDER,
    REPORT_GUIDANCE_PLACEHOLDER,
    REPORT_INITIAL_SNIPPETS_PLACEHOLDER,
)
from tests.harness_fixtures import MemoryStore


class _SqliteCursor:
    def __init__(self, rows=None, cursor=None):
        self._rows = rows
        self._cursor = cursor

    @staticmethod
    def _decode(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("document", "result", "data"):
            value = result.get(key)
            if isinstance(value, str):
                try:
                    result[key] = json.loads(value)
                except json.JSONDecodeError:
                    pass
        return result

    def fetchone(self):
        if self._rows is not None:
            return self._rows.pop(0) if self._rows else None
        return self._decode(self._cursor.fetchone())

    def fetchall(self):
        if self._rows is not None:
            rows, self._rows = self._rows, []
            return rows
        return [self._decode(row) for row in self._cursor.fetchall()]

    @property
    def lastrowid(self):
        return self._cursor.lastrowid if self._cursor is not None else None


class _SqliteRequestConnection:
    """RequestLedger 的合成 SQLite SQL 边界，不代表生产 PG 事务语义。"""

    def __init__(self, connection: sqlite3.Connection, store: "SqliteMemoryStore"):
        self._connection = connection
        self._store = store

    @staticmethod
    def _value(value):
        if isinstance(value, Jsonb):
            return json.dumps(value.obj, ensure_ascii=False, allow_nan=False)
        if isinstance(value, UUID):
            return str(value)
        return value

    def execute(self, sql, params=()):
        normalized = re.sub(r"\s+FOR\s+UPDATE\b", "", sql, flags=re.IGNORECASE)
        normalized = normalized.replace("clock_timestamp()", "CURRENT_TIMESTAMP")
        values = tuple(self._value(value) for value in params)
        if "COUNT(*) FILTER" in normalized and "FROM report_run_requests q" in normalized:
            return _SqliteCursor([self._store.aggregate_request_rows(values[0])])
        cursor = self._connection.execute(normalized.replace("%s", "?"), values)
        return _SqliteCursor(cursor=cursor)


class SqliteMemoryStore(MemoryStore):
    """用 MemoryStore 保存控制器状态，用 SQLite 保存合成请求账本。"""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE report_runs (
                    run_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    last_event_seq INTEGER NOT NULL,
                    document TEXT NOT NULL
                );
                CREATE TABLE report_run_requests (
                    request_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL UNIQUE,
                    fencing_token INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    endpoint_digest TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reserved_tokens INTEGER NOT NULL,
                    actual_tokens INTEGER,
                    result TEXT,
                    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
                    settled_at INTEGER
                );
                CREATE TABLE report_run_events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    state_version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );
                """
            )

    @staticmethod
    def _document(record):
        return {
            key: deepcopy(value)
            for key, value in record.items()
            if key not in {"state", "state_version", "last_event_seq"}
        }

    @contextmanager
    def _transaction(self):
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield _SqliteRequestConnection(connection, self)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def locked(self, owner, run_id, expected_version=None, fencing_token=None):
        record = self.get(owner, run_id)
        if expected_version is not None and record["state_version"] != expected_version:
            raise HarnessError("version_conflict")
        if fencing_token is not None:
            if self.tokens[run_id] != fencing_token or record["state"] in {
                "published", "needs_review", "cancelled", "failed", "suspended",
            }:
                raise HarnessError("lease_lost")
        row = {
            "run_id": run_id,
            "state": record["state"],
            "state_version": record["state_version"],
            "last_event_seq": record["last_event_seq"],
            "document": self._document(record),
        }
        with self._transaction() as connection:
            yield connection, row

    @contextmanager
    def locked_settlement(self, owner, run_id):
        record = self.get(owner, run_id)
        row = {
            "run_id": run_id,
            "state": record["state"],
            "state_version": record["state_version"],
            "last_event_seq": record["last_event_seq"],
            "document": self._document(record),
        }
        with self._transaction() as connection:
            yield connection, row

    def save(self, connection, row, state, document, event_type, data):
        current = self.records[row["run_id"]]
        current.update(deepcopy(document))
        current.update(
            state=state,
            state_version=current["state_version"] + 1,
            last_event_seq=current["last_event_seq"] + 1,
        )
        self.records[row["run_id"]] = current
        connection.execute(
            "UPDATE report_runs SET state=%s,state_version=%s,last_event_seq=%s,document=%s "
            "WHERE run_id=%s",
            (
                state,
                current["state_version"],
                current["last_event_seq"],
                json.dumps(self._document(current), ensure_ascii=False),
                row["run_id"],
            ),
        )
        connection.execute(
            "INSERT INTO report_run_events(run_id,seq,type,state_version,data) "
            "VALUES (%s,%s,%s,%s,%s)",
            (
                row["run_id"],
                current["last_event_seq"],
                event_type,
                current["state_version"],
                json.dumps(data, ensure_ascii=False),
            ),
        )
        self.event_log[row["run_id"]].append({
            "run_id": row["run_id"],
            "seq": current["last_event_seq"],
            "type": event_type,
            "state_version": current["state_version"],
            "occurred_at": "2026-01-01T00:00:00Z",
            "data": deepcopy(data),
        })
        return self.get(row["owner"], row["run_id"]) if "owner" in row else self.get(
            self.owners[row["run_id"]], row["run_id"],
        )

    def create(self, owner, request_id, request_digest, document):
        result = super().create(owner, request_id, request_digest, document)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "INSERT INTO report_runs(run_id,state,state_version,last_event_seq,document) "
                "VALUES (?,?,?,?,?)",
                (
                    result["run_id"],
                    result["state"],
                    result["state_version"],
                    result["last_event_seq"],
                    json.dumps(self._document(result), ensure_ascii=False),
                ),
            )
        return result

    def acquire(self, owner, run_id, expected_version, worker, *, max_active_runs=None):
        return super().acquire(owner, run_id, expected_version, worker)

    def aggregate_request_rows(self, run_id):
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT request_id,role,status,reserved_tokens,actual_tokens "
                "FROM report_run_requests WHERE run_id=?",
                (str(run_id),),
            ).fetchall()
        physical_states = {"dispatched", "committed", "completion_unknown"}
        capacity_states = {"intent", "dispatched", "committed", "completion_unknown"}
        unknown_rows = [
            row for row in rows
            if row["status"] == "completion_unknown"
            or (row["status"] == "committed" and row["actual_tokens"] is None)
        ]
        record = self.records[str(run_id)]
        approved = {str(item) for item in record.get("approved_unknown_request_ids", [])}
        return {
            "physical_requests": sum(row["status"] in physical_states for row in rows),
            "capacity_requests": sum(row["status"] in capacity_states for row in rows),
            "known_used": sum(
                row["actual_tokens"] or 0
                for row in rows
                if row["status"] == "committed" and row["actual_tokens"] is not None
            ),
            "unknown_reserved": sum(row["reserved_tokens"] for row in unknown_rows),
            "inflight_reserved": sum(
                row["reserved_tokens"]
                for row in rows
                if row["status"] in {"intent", "dispatched"}
                and row["actual_tokens"] is None
            ),
            "unknown_requests": len(unknown_rows),
            "unapproved_unknown_requests": sum(
                str(row["request_id"]) not in approved for row in unknown_rows
            ),
            "usage_exceeded": any(
                row["status"] == "committed"
                and row["actual_tokens"] is not None
                and row["actual_tokens"] > row["reserved_tokens"]
                for row in rows
            ),
            "retrieval_requests": sum(
                row["role"] == "embedding" and row["status"] in physical_states
                for row in rows
            ),
            "capacity_retrieval_requests": sum(
                row["role"] == "embedding" and row["status"] in capacity_states
                for row in rows
            ),
        }


class _RegistrationClient:
    registered_roles = ("embedding", "expert", "generator", "reviewer")

    async def close(self):
        return None


class _LegacyClient:
    registered_roles = ("generator", "reviewer")

    async def attempt(self, role, payload, timeout):
        return {"usage": {"cost": "0", "total_tokens": 1}}

    async def close(self):
        return None


class _SyntheticRetriever:
    _hybrid_available = True
    reranker_client = None
    initial_config = {"final_context_top_k": 3}
    agentic_config = {"final_context_top_k": 3}

    def __init__(self, embedding, chunks):
        self.embedding = embedding
        self.chunks = chunks
        self.calls = []

    def _run_hybrid(self, *, query, final_limit, config, enforce_type_balance, retrieval_mode):
        self.calls.append((query, retrieval_mode))
        self.embedding.embed_query(query)
        return deepcopy(self.chunks[:final_limit])


@contextmanager
def _loopback_server():
    calls = []
    report_text = "合成事故报告正文"

    def response_for(path, payload):
        if path == "/expert":
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {
                        "content": "思考过程\n```json\n{\"建议\":\"按合成事实审查\"}\n```",
                        "think": "message think must not be persisted",
                        "reasoning": "message reasoning must not be persisted",
                    },
                }],
                "think": "top-level think must not be persisted",
                "reasoning": "top-level reasoning must not be persisted",
                "usage": {"total_tokens": 7},
            }
        if path == "/embedding":
            return {
                "data": [{"index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"total_tokens": 4, "cost": "0.01"},
            }
        if path == "/generator":
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": json.dumps({
                        "version": 1,
                        "report_markdown": report_text,
                        "claims": [{
                            "claim_id": "claim-1",
                            "quote": report_text,
                            "type": "fact",
                            "evidence_refs": [],
                            "knowledge_refs": ["knowledge-1"],
                        }],
                        "obligation_resolutions": [],
                        "issue_responses": [],
                    }, ensure_ascii=False)},
                }],
                "usage": {"total_tokens": 9, "cost": "0.01"},
            }
        if path == "/reviewer":
            wire = json.loads(payload["messages"][1]["content"])
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": json.dumps({
                        "candidate_digest": wire["candidate_digest"],
                        "snapshot_digest": wire["snapshot_digest"],
                        "coverage_checks": [],
                        "issues": [],
                        "completed_checks": [{
                            "category": category,
                            "passed": True,
                            "evidence_refs": [],
                            "knowledge_refs": [],
                            "conclusion": "合成审查通过",
                        } for category in (
                            "facts", "coverage", "reasoning", "citations", "conciseness",
                        )],
                    }, ensure_ascii=False)},
                }],
                "usage": {"total_tokens": 8, "cost": "0.01"},
            }
        raise AssertionError(f"unexpected path: {path}")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "payload": payload})
            body = json.dumps(
                response_for(self.path, payload), ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _prompts():
    return BusinessWorkflow(
        f"原专家模板\n{GUIDANCE_ACCIDENT_PLACEHOLDER}",
        "|".join([
            REPORT_GUIDANCE_PLACEHOLDER,
            REPORT_ACCIDENT_PLACEHOLDER,
            REPORT_ACCIDENT_ANCHOR_PLACEHOLDER,
            REPORT_INITIAL_SNIPPETS_PLACEHOLDER,
            REPORT_ADDITIONAL_SNIPPETS_PLACEHOLDER,
            REPORT_AGENTIC_HISTORY_PLACEHOLDER,
        ]),
        initial_top_k=1,
        additional_rounds=0,
        additional_top_k=1,
        max_total_snippets=1,
    )


def _capacity(role, model, output, effort):
    return ModelCapacity(
        model=model,
        context_tokens=64,
        output_tokens=output,
        proof_digest=canonical_digest({"role": role, "model": model}),
        effort=effort,
    )


def _contract(role, capacity, endpoint, quote, mode, usage_source):
    return RoleBillingContract(
        role=role,
        model=capacity.model,
        endpoint_digest=canonical_digest({
            "role": role,
            "endpoint": endpoint,
            "model": capacity.model,
            "capacity_proof_digest": capacity.proof_digest,
        }),
        quote_cny=Decimal(quote),
        billing_mode=mode,
        usage_source=usage_source,
    )


def test_business_runtime_uses_real_loopback_http_and_shared_ledgers(tmp_path):
    async def execute():
        with _loopback_server() as (base_url, calls):
            endpoints = {
                role: f"{base_url}/{role}"
                for role in ("expert", "embedding", "generator", "reviewer")
            }
            capacities = {
                "expert": _capacity("expert", "synthetic-expert", 64, None),
                "embedding": _capacity("embedding", "synthetic-embedding", 0, None),
                "generator": _capacity("generator", ALLOWED_MODEL, 16, "high"),
                "reviewer": _capacity("reviewer", ALLOWED_MODEL, 16, "high"),
            }
            chunks = ({
                "id": "knowledge-1",
                "document_id": "synthetic-doc",
                "version": "v1",
                "text": "合成道路安全规则",
                "digest": canonical_digest("合成道路安全规则"),
                "manifest_digest": "pending",
            },)
            collection = KnowledgeCollection(
                collection_id="synthetic-knowledge",
                version="v1",
                content_digest=canonical_digest(chunks),
                label="合成知识",
            )
            manifest_digest = canonical_digest([collection.model_dump(mode="json")])
            chunks = tuple({**chunk, "manifest_digest": manifest_digest} for chunk in chunks)
            catalog = AuthorizationCatalog([
                EndpointDescription(
                    role=role,
                    label=f"回环 {role}",
                    base_url=endpoints[role],
                    model=capacities[role].model,
                    version=capacities[role].proof_digest,
                )
                for role in ("expert", "embedding", "generator", "reviewer")
            ], [collection], frozenset({manifest_digest}))
            workflow = _prompts()
            budget_policy = BudgetPolicy(
                max_physical_requests=16,
                max_retrieval_requests=2,
                max_revision_rounds=0,
                max_output_tokens_per_request=8192,
            )
            money_path = tmp_path / "synthetic-money.sqlite3"
            seed = MoneyGuardHTTPAttemptClient(
                _LegacyClient(), money_path, "report-evidence-v1-Q-CNY100",
                Decimal("1"), Decimal("7"), model=ALLOWED_MODEL,
                endpoint_summary={"synthetic": "business-runtime"},
            )
            await seed.close()
            registration = VersionedMoneyGuardHTTPAttemptClient(
                _RegistrationClient(), money_path, "report-evidence-v1-Q-CNY100",
            )
            registration.register_contract(
                _contract(
                    "expert", capacities["expert"], endpoints["expert"],
                    "0", "local_token_free", "total_tokens",
                ), registration.billing_contract_digest,
            )
            registration.register_contract(
                _contract(
                    "embedding", capacities["embedding"], endpoints["embedding"],
                    "1", "remote_actual", "cost_usd",
                ), registration.billing_contract_digest,
            )
            await registration.close()
            billing = read_billing_contract(
                money_path, "report-evidence-v1-Q-CNY100",
            )

            captured_guards = []

            def monetary_factory(raw_client):
                guard = VersionedMoneyGuardHTTPAttemptClient(
                    raw_client,
                    money_path,
                    "report-evidence-v1-Q-CNY100",
                    contracts=billing["contracts"],
                    usd_to_cny_upper=billing["usd_to_cny_upper"],
                )
                captured_guards.append(guard)
                return guard

            retrievers = []

            def retriever_factory(embedding):
                retriever = _SyntheticRetriever(embedding, chunks)
                retrievers.append(retriever)
                return retriever

            dependencies = build_business_dependencies(
                workflow=workflow,
                capacities=capacities,
                endpoints=endpoints,
                headers={},
                catalog=catalog,
                knowledge_chunks=chunks,
                budget_policy=budget_policy,
                monetary_client_factory=monetary_factory,
                retriever_factory=retriever_factory,
                validate_source=lambda: None,
                embedding_dimensions=2,
                billing_contract_digest=billing["billing_contract_digest"],
                resource_check=lambda: None,
            )
            store = SqliteMemoryStore(tmp_path / "synthetic-request.sqlite3")
            service = ReportRunService(store, dependencies)
            run = service.create(
                "owner",
                CreateRunRequest(
                    request_id=uuid4(),
                    session_id="synthetic-business-runtime",
                    accident_data={
                        "事故标题": "合成路口事故",
                        "事故经过": "车辆在路口发生碰撞",
                    },
                    evidence_revision=0,
                ),
            )
            preview = service.authorization_preview("owner", run["run_id"])
            approved = service.authorize(
                "owner",
                run["run_id"],
                AuthorizationRequest(
                    snapshot_digest=preview["snapshot_digest"],
                    endpoint_profile_digest=preview["endpoint_profile_digest"],
                    approved_knowledge_manifest_digest=preview[
                        "approved_knowledge_manifest_digest"
                    ],
                    confirmed=True,
                ),
            )
            token = service.claim("owner", run["run_id"], approved["state_version"])
            roles = await dependencies.runtime_roles_factory(
                store, "owner", run["run_id"], token,
            )
            try:
                snapshot = store.get("owner", run["run_id"])["snapshot"]
                prepared = await roles.prepare(deepcopy(snapshot))
                initial = await asyncio.to_thread(
                    roles.retrieve_initial_knowledge,
                    workflow.initial_query(snapshot["accident_data"]),
                    1,
                )
                candidate = await roles.generate({
                    "instructions": "harness generator instructions",
                    "response_schema": CandidateReport.model_json_schema(),
                    "snapshot": deepcopy(snapshot),
                    "prepared": deepcopy(prepared),
                    "initial_knowledge_snippets": deepcopy(initial),
                    "tool_results": [],
                    "agentic_rounds": [],
                    "candidate_version": 1,
                    "previous_candidate": None,
                    "unresolved_issues": [],
                    "review_feedback": None,
                })
                candidate_digest = canonical_digest(candidate)
                review = await roles.review({
                    "instructions": "harness reviewer instructions",
                    "response_schema": ReviewResult.model_json_schema(),
                    "snapshot": deepcopy(snapshot),
                    "snapshot_digest": canonical_digest(snapshot),
                    "candidate": deepcopy(candidate),
                    "candidate_digest": candidate_digest,
                    "unresolved_issues": [],
                    "prepared": {"secret": "expert history must stay private"},
                    "initial_knowledge_snippets": [{"secret": "generator history"}],
                    "generator_history": [{"secret": "generator history"}],
                })
            finally:
                await roles.close()

            assert prepared == {
                "guidance": {"建议": "按合成事实审查"},
                "knowledge": [],
            }
            assert initial == [chunks[0]]
            assert candidate["claims"][0]["text_span"] == {
                "start": 0, "end": len("合成事故报告正文"),
            }
            assert review["candidate_digest"] == candidate_digest
            assert len(retrievers) == 1 and retrievers[0].calls
            assert len(captured_guards) == 1
            guard = captured_guards[0]
            assert guard.billing_contract_digest == billing["billing_contract_digest"]
            assert guard.registered_roles == ("embedding", "expert", "generator", "reviewer")

            by_path = {item["path"]: item["payload"] for item in calls}
            assert set(by_path) == {f"/{role}" for role in endpoints}
            expert_payload = by_path["/expert"]
            assert "reasoning" not in expert_payload
            assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & expert_payload.keys()
            assert "需要工具时" not in json.dumps(expert_payload, ensure_ascii=False)
            assert "原专家模板" in expert_payload["messages"][1]["content"]
            assert by_path["/embedding"].keys() == {"model", "input"}
            for role in ("/generator", "/reviewer"):
                assert by_path[role]["reasoning"] == {"effort": "high", "exclude": True}
                assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & by_path[role].keys()
            generator_wire = json.dumps(by_path["/generator"], ensure_ascii=False)
            assert "按合成事实审查" in generator_wire
            assert "合成道路安全规则" in generator_wire
            reviewer_wire = json.dumps(by_path["/reviewer"], ensure_ascii=False)
            assert "expert history must stay private" not in reviewer_wire
            assert "generator history" not in reviewer_wire

            request_view = RequestLedger(store).view("owner", run["run_id"])
            assert request_view["physical_requests"] == 4
            assert request_view["retrieval_requests"] == 1
            assert request_view["known_used"] == 28
            assert request_view["unknown_reserved"] == 0
            assert request_view["inflight_reserved"] == 0

            with sqlite3.connect(money_path) as connection:
                money_rows = connection.execute(
                    "SELECT experiment_id,role,usage_total_tokens,usage_cost_usd,state,"
                    "contract_digest,committed_micro FROM money_guard_attempts "
                    "ORDER BY attempt_id"
                ).fetchall()
                experiment_count = connection.execute(
                    "SELECT COUNT(*) FROM money_guard_experiments"
                ).fetchone()[0]
            assert experiment_count == 1
            assert [row[1] for row in money_rows] == [
                "expert", "embedding", "generator", "reviewer",
            ]
            assert all(row[0] == "report-evidence-v1-Q-CNY100" for row in money_rows)
            assert all(row[4] == "settled" for row in money_rows)
            assert all(row[5] == billing["billing_contract_digest"] for row in money_rows)
            assert money_rows[0][2:4] == (7, None)
            assert money_rows[1][2:4] == (4, "0.01")
            assert money_rows[2][2:4] == (9, "0.01")
            assert money_rows[3][2:4] == (8, "0.01")
            assert money_rows[0][6] == 0
            assert all(row[6] > 0 for row in money_rows[1:])

            with sqlite3.connect(store.path) as connection:
                persisted_expert = json.loads(connection.execute(
                    "SELECT result FROM report_run_requests WHERE role='expert'"
                ).fetchone()[0])
            persisted_message = persisted_expert["choices"][0]["message"]
            assert persisted_expert["usage"] == {"total_tokens": 7}
            assert all(field not in persisted_expert for field in (
                "think", "reasoning", "reasoning_content", "thinking",
            ))
            assert all(field not in persisted_message for field in (
                "think", "reasoning", "reasoning_content", "thinking",
            ))
            assert "思考过程" not in persisted_message["content"]
            assert "```" not in persisted_message["content"]
            assert json.loads(persisted_message["content"]) == {
                "建议": "按合成事实审查",
            }

    asyncio.run(execute())
