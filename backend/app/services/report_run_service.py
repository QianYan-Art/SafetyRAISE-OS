from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError
from psycopg.rows import dict_row

from app.adapters.input.dict_input_adapter import DictInputAdapter
from app.report_harness.contracts import canonical_digest, validate_publication, validate_review_structure
from app.report_harness.authorization import AuthorizationRequest
from app.report_harness.evidence import freeze_snapshot
from app.report_harness.errors import HarnessError
from app.report_harness.execution import ReportExecutionDependencies
from app.report_harness.controlled_tools import ControlledTools
from app.report_harness.role_loop import RoleLoop, role_context, tool_schemas
from app.report_harness.journal import ExecutionJournal, JOURNAL_VERSION
from app.report_harness.recovery import RunRecovery
from app.report_harness.review_ledger import IssueLedger
from app.report_harness.prompts import load_role_prompts
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.release_registry import export_eligibility
from app.report_harness.store import RunStore
from app.schemas.report import ReportResult
from app.schemas.report_run import CandidateReport, CreateRunRequest, ReviewResult

logger = logging.getLogger(__name__)

class ReportRunService:
    def __init__(self, store: RunStore, dependencies: ReportExecutionDependencies):
        self.store = store
        self.dependencies = dependencies

    def create(self, owner: str, request: CreateRunRequest) -> dict:
        self.store.check_schema()
        data = deepcopy(DictInputAdapter(request.accident_data).load())
        snapshot = freeze_snapshot(data, [], request.evidence_revision,
                                   self.dependencies.knowledge_manifest_digest)
        document = {
            "run_id": str(uuid4()), "session_id": request.session_id,
            "parent_run_id": str(request.parent_run_id) if request.parent_run_id else None,
            "evidence_revision": request.evidence_revision,
            "snapshot": snapshot, "snapshot_digest": canonical_digest(snapshot),
            "candidate_version": 0, "review_status": "pending",
            "terminal_reason": None, "quality_gate": "engineering_only",
            "formal_export_eligible": False, "release_binding_status": "unapproved",
            "budget": {"physical_requests": 0, "known_used": 0, "unknown_reserved": 0,
                       "inflight_reserved": 0,
                       "remaining": self.dependencies.budget_policy.max_total_tokens},
            "budget_policy": self.dependencies.budget_policy.model_dump(mode="json"),
            "active_seconds": 0,
            "execution_profile": self.dependencies.execution_profile,
            "endpoint_profile_digest": self.dependencies.endpoint_profile_digest,
            "policy_digest": self.dependencies.policy_digest,
            "knowledge_source": list(deepcopy(self.dependencies.knowledge_chunks)),
            "knowledge_source_digest": canonical_digest(self.dependencies.knowledge_chunks),
            "role_prompts": load_role_prompts(),
            "execution_contract_digest": self._contract_digest(),
            "approval": None,
        }
        registry = self.dependencies.release_registry
        if (self.dependencies.execution_profile == "outbound"
                and self.dependencies.code_digest and registry is not None):
            binding = registry.binding_for({
                "code_digest": self.dependencies.code_digest,
                "policy_digest": self.dependencies.policy_digest,
                "model_endpoint_digest": self.dependencies.endpoint_profile_digest,
                "knowledge_manifest_digest": self.dependencies.knowledge_manifest_digest,
            })
            if binding:
                document["release_binding"] = binding
                document["quality_gate"] = "quality_validated"
        result = self.store.create(
            owner, request.request_id, canonical_digest(request.model_dump(mode="json")), document
        )
        return self._budget_view(owner, result)

    @staticmethod
    def public_view(document: dict) -> dict:
        keys = (
            "run_id", "session_id", "state", "state_version", "snapshot_digest",
            "candidate_version", "review_status", "terminal_reason", "budget",
            "last_event_seq", "quality_gate", "formal_export_eligible", "release_binding_status",
        )
        result = {key: document[key] for key in keys}
        result["execution_profile"] = document.get("execution_profile", "outbound")
        result["budget_policy"] = document.get("budget_policy", {})
        if document["state"] == "published":
            result["report"] = document["report"]
        return result

    def get(self, owner: str, run_id: str) -> dict:
        record = (RunRecovery(self.store).recover_expired(owner, run_id)
                  if hasattr(self.store, "connection") else self.store.get(owner, run_id))
        return self._budget_view(owner, record)

    @staticmethod
    def _contract_digest() -> str:
        return canonical_digest({
            "controller_version": 1, "journal_version": JOURNAL_VERSION,
            "candidate": CandidateReport.model_json_schema(),
            "review": ReviewResult.model_json_schema(), "tools": tool_schemas(),
        })

    def _budget_view(self, owner: str, record: dict) -> dict:
        result = self.public_view(record)
        eligible, status = export_eligibility(record, self.dependencies.release_registry)
        result["formal_export_eligible"] = eligible
        result["release_binding_status"] = status
        if "budget_policy" in record and hasattr(self.store, "connection"):
            result["budget"] = {
                **RequestLedger(self.store).view(owner, record["run_id"]),
                "active_seconds": record.get("active_seconds", 0),
            }
        return result

    def candidate(self, owner: str, run_id: str) -> dict:
        record = self.store.get(owner, run_id)
        if not record.get("candidate"):
            raise HarnessError("not_found", 404)
        return {
            "candidate_version": record["candidate_version"],
            "snapshot_digest": record["snapshot_digest"],
            "candidate_report": record["candidate"],
            "review_result": record.get("review"),
            "display_status": "candidate",
        }

    def authorization_preview(self, owner: str, run_id: str) -> dict:
        record = self.store.get(owner, run_id)
        catalog = self.dependencies.authorization_catalog
        if catalog is None:
            return {
                "available": False, "reason": "authorization_profile_unavailable",
                "snapshot_digest": record["snapshot_digest"],
                "endpoint_profile_digest": record["endpoint_profile_digest"],
                "approved_knowledge_manifest_digest": record["snapshot"]["knowledge_manifest_digest"],
                "snapshot": deepcopy(record["snapshot"]), "endpoints": [],
                "knowledge_collections": [],
            }
        return catalog.preview(record)

    def authorize(self, owner: str, run_id: str, request: AuthorizationRequest) -> dict:
        with self.store.locked(owner, run_id) as (conn, row):
            if row["state"] not in {"queued", "suspended"}:
                raise HarnessError("authorization_state_conflict")
            catalog = self.dependencies.authorization_catalog
            if catalog is None:
                raise HarnessError("authorization_profile_unavailable")
            approval = catalog.validate(row["document"], request)
            approval.update(policy_digest=row["document"]["policy_digest"], owner_user_id=owner)
            previous = row["document"].get("approval")
            if previous and all(previous.get(key) == value for key, value in approval.items()):
                updated = self.store._view(row)
            else:
                approval["approved_at"] = datetime.now(timezone.utc).isoformat()
                updated = self.store.save(
                    conn, row, row["state"], {**row["document"], "approval": approval},
                    "checkpoint", {"step": "authorization", "snapshot_digest": request.snapshot_digest},
                )
        return self._budget_view(owner, updated)

    def preflight(self, owner: str, run_id: str, expected_version: int) -> None:
        record = self.store.get(owner, run_id)
        if record["state_version"] != expected_version:
            raise HarnessError("version_conflict")
        if record["state"] != "queued":
            raise HarnessError("not_executable")
        self._validate_profile(record, owner)

    def _validate_profile(self, record: dict, owner: str | None = None) -> None:
        profile = record.get("execution_profile")
        if profile == "outbound" and not self.dependencies.development_outbound_enabled:
            # 授权和计费边界完成前，在线执行始终关闭，不能借工程标记外发。
            if record.get("approval"):
                raise HarnessError("outbound_transport_unavailable", 503)
            raise HarnessError("authorization_required")
        if profile not in {"synthetic_test", "outbound"}:
            raise HarnessError("authorization_stale")
        if (self.dependencies.execution_profile != profile
                or record.get("quality_gate") != "engineering_only"
                or record["endpoint_profile_digest"] != self.dependencies.endpoint_profile_digest
                or record["policy_digest"] != self.dependencies.policy_digest
                or record["snapshot"]["knowledge_manifest_digest"]
                != self.dependencies.knowledge_manifest_digest
                or record.get("knowledge_source_digest")
                != canonical_digest(self.dependencies.knowledge_chunks)
                or record.get("role_prompts") != load_role_prompts()):
            raise HarnessError("authorization_stale")
        if record.get("budget_policy") != self.dependencies.budget_policy.model_dump(mode="json"):
            raise HarnessError("authorization_stale")
        if (record.get("execution_contract_digest") != self._contract_digest()
                or canonical_digest(record["snapshot"]) != record["snapshot_digest"]):
            raise HarnessError("checkpoint_version_mismatch")
        if profile == "outbound":
            self._validate_development_outbound(record, owner)

    def _validate_development_outbound(self, record: dict, owner: str | None) -> None:
        if record.get("release_binding") is not None or record.get("formal_export_eligible", False):
            raise HarnessError("authorization_stale")
        catalog = self.dependencies.authorization_catalog
        if catalog is None or not catalog.preview(record)["available"]:
            raise HarnessError("authorization_stale")
        approval = record.get("approval")
        if (not isinstance(approval, dict)
                or not isinstance(approval.get("approved_at"), str)
                or not approval["approved_at"].strip()):
            raise HarnessError("authorization_required")
        expected = {
            "snapshot_digest": record["snapshot_digest"],
            "endpoint_profile_digest": record["endpoint_profile_digest"],
            "approved_knowledge_manifest_digest": record["snapshot"]["knowledge_manifest_digest"],
            "policy_digest": record["policy_digest"],
            "owner_user_id": owner,
        }
        if any(approval.get(key) != value for key, value in expected.items()):
            raise HarnessError("authorization_stale")

    def claim(self, owner: str, run_id: str, expected_version: int) -> int:
        self.preflight(owner, run_id, expected_version)
        return self.store.acquire(owner, run_id, expected_version, uuid4())

    def resume_claim(self, owner: str, run_id: str, expected_version: int, *,
                     retry_unknown_requests: bool = False) -> int:
        self.store.check_schema()
        RunRecovery(self.store).recover_expired(owner, run_id)
        record = self.store.get(owner, run_id)
        self._validate_profile(record, owner)
        minimum_requests, minimum_tokens = self._resume_minimum(record)

        def validate(document):
            self._validate_profile(document, owner)
            limit = min(self.dependencies.max_active_seconds,
                        self.dependencies.budget_policy.max_active_seconds)
            if document.get("active_seconds", 0) >= limit:
                raise HarnessError("budget_exhausted")

        return RunRecovery(self.store).resume_claim(
            owner, run_id, expected_version, uuid4(),
            retry_unknown_requests=retry_unknown_requests, validate=validate,
            minimum_requests=minimum_requests, minimum_tokens=minimum_tokens,
        )

    def _resume_minimum(self, record: dict) -> tuple[int, int]:
        with self.store.connection() as conn:
            conn.row_factory = dict_row
            pending = conn.execute(
                "SELECT role,reserved_tokens FROM report_run_requests WHERE run_id=%s "
                "AND (status='completion_unknown' OR (status='committed' AND actual_tokens IS NULL)) "
                "ORDER BY created_at DESC,request_id DESC LIMIT 1", (record["run_id"],),
            ).fetchone()
        if pending is None:
            # 已有结果可先恢复本地计算；任何新HTTP仍在发送前做完整预留。
            return 0, 0
        profile = record.get("runtime_budget_profile")
        if not isinstance(profile, dict):
            raise HarnessError("checkpoint_version_mismatch")
        role, amount = pending["role"], pending["reserved_tokens"]
        if role == "reviewer":
            return 1, max(amount, profile["review_reserve_tokens"])
        if role == "generator":
            return 2, max(amount, profile["generation_reserve_tokens"]) + profile["review_reserve_tokens"]
        return 3, amount + profile["generation_reserve_tokens"] + profile["review_reserve_tokens"]

    async def execute(self, owner: str, run_id: str, expected_version: int) -> dict:
        return await self.execute_claimed(owner, run_id, self.claim(owner, run_id, expected_version))

    async def execute_claimed(self, owner: str, run_id: str, token: int) -> dict:
        roles = None
        controller = asyncio.current_task()
        heartbeat_failed = False

        async def heartbeat():
            nonlocal heartbeat_failed
            next_heartbeat = asyncio.get_running_loop().time() + 5
            try:
                while True:
                    await asyncio.sleep(0.25)
                    await asyncio.to_thread(self.store.assert_active, owner, run_id, token)
                    now = asyncio.get_running_loop().time()
                    if now >= next_heartbeat:
                        await asyncio.to_thread(self.store.heartbeat, owner, run_id, token)
                        next_heartbeat = now + 5
            except Exception:
                heartbeat_failed = True
                logger.warning("报告运行心跳失效，停止当前执行。", extra={"run_id": run_id})
                controller.cancel()

        pulse = asyncio.create_task(heartbeat())
        try:
            self.store.assert_active(owner, run_id, token)
            if self.dependencies.runtime_roles_factory is None:
                roles = self.dependencies.roles_factory()
            else:
                roles = self.dependencies.runtime_roles_factory(self.store, owner, run_id, token)
            policy = self.dependencies.budget_policy
            active_used = self.store.get(owner, run_id).get("active_seconds", 0)
            timeout = min(self.dependencies.max_active_seconds, policy.max_active_seconds) - active_used
            if timeout <= 0:
                raise HarnessError("budget_exhausted")
            async with asyncio.timeout(timeout):
                record = self.store.get(owner, run_id)
                snapshot = deepcopy(record["snapshot"])
                tools = ControlledTools(snapshot, deepcopy(record["knowledge_source"]))
                journal = (ExecutionJournal(self.store, owner, run_id, token)
                           if hasattr(self.store, "connection") else None)
                tool_history = []

                def checkpoint(public, private):
                    tool_history.append({**deepcopy(public), "private": deepcopy(private)})
                    current = self.store.get(owner, run_id)
                    self.store.transition(
                        owner, run_id, token, current["state"],
                        {"tool_history": deepcopy(tool_history),
                         "knowledge_registry": tools.registered_knowledge()},
                        "tool", public,
                    )

                loop = RoleLoop(
                    tools, checkpoint, max_tool_calls=policy.max_tool_calls,
                    before_call=lambda: self.store.assert_active(owner, run_id, token),
                    journal=journal, replay_model=getattr(roles, "replay", None),
                )
                self.store.assert_active(owner, run_id, token)
                if journal is None:
                    prepared = await roles.prepare(deepcopy(snapshot))
                else:
                    replay_prepare = getattr(roles, "replay_prepare", None)
                    prepared = await journal.invoke(
                        "prepare", {"snapshot": snapshot, "contract": record["execution_contract_digest"]},
                        lambda: roles.prepare(deepcopy(snapshot)), limit=24,
                        replay_operation=(
                            (lambda: replay_prepare(deepcopy(snapshot))) if replay_prepare else None
                        ),
                    )
                if set(prepared) != {"guidance", "knowledge"} or prepared["knowledge"]:
                    raise HarnessError("untrusted_prepared_knowledge", 422)
                self.store.transition(owner, run_id, token, "generating",
                                      {"prepared": prepared,
                                       "knowledge_registry": tools.registered_knowledge()},
                                      "checkpoint", {"step": "prepared"})
                ledger = IssueLedger(namespace=run_id if journal is not None else None)
                previous = None
                feedback = None
                max_version = min(2, policy.max_revision_rounds) + 1
                for version in range(1, max_version + 1):
                    candidate, review, check_args = await self._candidate_round(
                        owner, run_id, token, record=record, prepared=prepared, roles=roles,
                        tools=tools, loop=loop, ledger=ledger, version=version, previous=previous,
                        feedback=feedback,
                    )
                    blocking = any(
                        item.severity in {"major", "blocker"} and item.status != "resolved"
                        for item in review.issues
                    )
                    passed = all(item.passed for item in [
                        *review.coverage_checks, *review.completed_checks,
                    ])
                    if passed and not blocking:
                        validate_publication(
                            candidate, review, *check_args,
                            resolved_issue_ids=frozenset(ledger.resolved_ids()),
                        )
                        break
                    if version == max_version:
                        self._stop_if_owned(owner, run_id, token, "needs_review",
                                            "revision_rounds_exhausted")
                        return self.get(owner, run_id)
                    previous = candidate.model_dump(mode="json")
                    feedback = review.model_dump(mode="json")
                    self.store.transition(
                        owner, run_id, token, "revising",
                        {"revision_round": version, "review_status": "revision_required"},
                        "stage", {"stage": "revising", "revision_round": version},
                    )
                candidate_data = candidate.model_dump(mode="json")
                review_data = review.model_dump(mode="json")
                published = self._publish(
                    owner, run_id, token, {
                        "review_status": "passed",
                        "report": ReportResult(report_markdown=candidate.report_markdown).model_dump(),
                        "publication": {
                            "candidate_digest": canonical_digest(candidate_data),
                            "review_digest": canonical_digest(review_data),
                            "snapshot_digest": record["snapshot_digest"],
                        },
                    },
                )
                return self._budget_view(owner, published)
        except asyncio.CancelledError:
            if not heartbeat_failed:
                self.cancel_claimed(owner, run_id, token)
            raise
        except (ValidationError, ValueError, TimeoutError) as exc:
            reason = "budget_exhausted" if isinstance(exc, TimeoutError) else "invalid_review_or_candidate"
            self._stop_if_owned(owner, run_id, token, "needs_review", reason)
            return self.get(owner, run_id)
        except HarnessError as exc:
            if exc.code in {"usage_unknown", "completion_unknown", "authorization_stale",
                            "authorization_required", "token_bound_unverified"}:
                self._stop_if_owned(owner, run_id, token, "suspended", exc.code)
                return self.get(owner, run_id)
            if exc.code in {"role_response_too_large", "invalid_role_response",
                            "model_turn_budget_exhausted", "tool_budget_exhausted",
                            "budget_exhausted", "usage_exceeded", "physical_request_budget_exhausted",
                            "retrieval_request_budget_exhausted", "token_budget_exhausted"}:
                reason = "budget_exhausted" if exc.code.endswith("budget_exhausted") else exc.code
                self._stop_if_owned(owner, run_id, token, "needs_review", reason)
                return self.get(owner, run_id)
            if exc.code != "lease_lost":
                self._stop_if_owned(owner, run_id, token, "failed", exc.code)
            raise
        except Exception as exc:
            logger.error("报告执行发生内部异常。", extra={
                "run_id": run_id, "error_type": type(exc).__name__,
            })
            self._stop_if_owned(owner, run_id, token, "failed", "execution_error")
            raise
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            if roles is not None:
                try:
                    await asyncio.wait_for(roles.close(), timeout=5)
                except Exception as exc:
                    logger.warning("报告角色资源关闭失败。", extra={
                        "run_id": run_id, "error_type": type(exc).__name__,
                    })

    async def _candidate_round(self, owner, run_id, token, *, record, prepared, roles,
                               tools, loop, ledger, version, previous, feedback):
        snapshot = record["snapshot"]
        context_snapshot, inline_evidence = role_context(snapshot)
        tools.reset_access("generator")
        candidate = CandidateReport.model_validate(await loop.run("generator", roles.generate, {
            "instructions": record["role_prompts"]["generator"],
            "response_schema": CandidateReport.model_json_schema(),
            "snapshot": deepcopy(context_snapshot), "prepared": deepcopy(prepared),
            "candidate_version": version, "previous_candidate": deepcopy(previous),
            "unresolved_issues": ledger.unresolved(),
            "review_feedback": deepcopy(feedback),
        }))
        if candidate.version != version:
            raise ValueError("候选版本不匹配。")
        candidate_data = candidate.model_dump(mode="json")
        generator_evidence = inline_evidence | tools.accessed_evidence("generator")
        generator_knowledge = tools.accessed_knowledge("generator")
        for response in candidate.issue_responses:
            if not set(response.source_refs) <= generator_evidence | generator_knowledge:
                raise ValueError("生成者回应引用了未取得原文的来源。")
        ledger.record_responses(candidate.issue_responses)
        current = self.store.get(owner, run_id)
        candidate_history = self._record_version(current.get("candidate_history", []), {
            "version": version, "digest": canonical_digest(candidate_data),
            "candidate": deepcopy(candidate_data),
        }, "version")
        self.store.transition(
            owner, run_id, token, "checking",
            {"candidate": candidate_data, "candidate_version": version,
             "issue_history": ledger.history(), "candidate_history": candidate_history},
            "checkpoint", {"step": "candidate", "digest": canonical_digest(candidate_data)},
        )
        # 每个版本重新构造审查上下文，不传生成者私有历史、自评或专家指导。
        tools.reset_access("reviewer")
        review = ReviewResult.model_validate(await loop.run("reviewer", roles.review, {
            "instructions": record["role_prompts"]["reviewer"],
            "response_schema": ReviewResult.model_json_schema(),
            "snapshot": deepcopy(context_snapshot), "snapshot_digest": record["snapshot_digest"],
            "candidate": deepcopy(candidate_data), "candidate_digest": canonical_digest(candidate_data),
            "unresolved_issues": ledger.unresolved(),
        }))
        self.store.transition(owner, run_id, token, "checking",
                              {"review": review.model_dump(mode="json")},
                              "review", {"candidate_version": version})
        review = ledger.apply(review)
        obligations = snapshot["fact_obligations"]
        reviewer_evidence = inline_evidence | tools.accessed_evidence("reviewer")
        for claim in candidate.claims:
            if (not set(claim.evidence_refs) <= generator_evidence
                    or not set(claim.knowledge_refs) <= tools.accessed_knowledge("generator")):
                raise ValueError("生成者引用了未取得原文的来源。")
        if not {ref for item in obligations for ref in item["source_refs"]} <= reviewer_evidence:
            raise ValueError("审查者尚未取得全部必要事实原文。")
        check_args = (
            record["snapshot_digest"], {item["obligation_id"] for item in obligations},
            reviewer_evidence, tools.accessed_knowledge("reviewer"),
        )
        validate_review_structure(candidate, review, *check_args,
                                  resolved_issue_ids=frozenset(ledger.resolved_ids()))
        current = self.store.get(owner, run_id)
        review_history = self._record_version(current.get("review_history", []), {
            "candidate_version": version, "candidate_digest": review.candidate_digest,
            "review": review.model_dump(mode="json"),
        }, "candidate_version")
        self.store.transition(
            owner, run_id, token, "checking",
            {"review": review.model_dump(mode="json"), "issue_history": ledger.history(),
             "review_history": review_history},
            "checkpoint", {"step": "review_validated", "candidate_version": version},
        )
        return candidate, review, check_args

    @staticmethod
    def _record_version(history: list[dict], entry: dict, key: str) -> list[dict]:
        existing = [item for item in history if item[key] == entry[key]]
        if existing:
            if existing != [entry]:
                raise HarnessError("checkpoint_digest_mismatch")
            return history
        return [*history, entry]

    def _publish(self, owner: str, run_id: str, token: int, patch: dict) -> dict:
        if not hasattr(self.store, "connection"):
            return self.store.transition(owner, run_id, token, "published", patch,
                                         "final", {"state": "published"})
        with self.store.locked(owner, run_id, fencing_token=token) as (conn, row):
            policy = RequestLedger._load_policy(row, reject_money=True)
            summary = RequestLedger._aggregate(
                conn, row["run_id"], max_total_tokens=policy.max_total_tokens,
            )
            RequestLedger._reject_blocked(summary)
            if (summary["remaining"] < 0 or RunStore._active_seconds(row) >= min(
                    policy.max_active_seconds, self.dependencies.max_active_seconds)):
                raise HarnessError("budget_exhausted")
            publication_budget = {
                **summary, "active_seconds": RunStore._active_seconds(row),
            }
            return self.store.save(
                conn, row, "published", {
                    **row["document"], **patch, "publication_budget": publication_budget,
                },
                "final", {"state": "published", "budget": publication_budget},
            )

    def _stop_if_owned(self, owner: str, run_id: str, token: int,
                       state: str, reason: str) -> None:
        try:
            self.store.transition(owner, run_id, token, state,
                                  {"terminal_reason": reason, "review_status": "failed"},
                                  "error", {"reason": reason})
        except HarnessError as exc:
            # 取消或租约抢占已经建立更强屏障，不覆盖其终态。
            if exc.code not in {"lease_lost", "not_found"}:
                raise

    def cancel(self, owner: str, run_id: str) -> dict:
        return self._budget_view(owner, self.store.cancel(owner, run_id))

    def cancel_claimed(self, owner: str, run_id: str, token: int) -> dict | None:
        try:
            return self._budget_view(owner, self.store.cancel(owner, run_id, expected_token=token))
        except HarnessError as exc:
            if exc.code != "not_found":
                raise
            return None
