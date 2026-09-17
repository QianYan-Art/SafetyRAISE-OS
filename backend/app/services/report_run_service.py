from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from pydantic import ValidationError

from app.adapters.input.dict_input_adapter import DictInputAdapter
from app.report_harness.contracts import canonical_digest, validate_publication
from app.report_harness.authorization import AuthorizationRequest
from app.report_harness.evidence import freeze_snapshot
from app.report_harness.errors import HarnessError
from app.report_harness.execution import ReportExecutionDependencies
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
                       "inflight_reserved": 0, "remaining": 120000},
            "execution_profile": self.dependencies.execution_profile,
            "endpoint_profile_digest": self.dependencies.endpoint_profile_digest,
            "policy_digest": self.dependencies.policy_digest,
            "approval": None,
        }
        result = self.store.create(
            owner, request.request_id, canonical_digest(request.model_dump(mode="json")), document
        )
        return self.public_view(result)

    @staticmethod
    def public_view(document: dict) -> dict:
        keys = (
            "run_id", "session_id", "state", "state_version", "snapshot_digest",
            "candidate_version", "review_status", "terminal_reason", "budget",
            "last_event_seq", "quality_gate", "formal_export_eligible", "release_binding_status",
        )
        result = {key: document[key] for key in keys}
        if document["state"] == "published":
            result["report"] = document["report"]
        return result

    def get(self, owner: str, run_id: str) -> dict:
        return self.public_view(self.store.get(owner, run_id))

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
                return self.public_view(self.store._view(row))
            approval["approved_at"] = datetime.now(timezone.utc).isoformat()
            updated = self.store.save(
                conn, row, row["state"], {**row["document"], "approval": approval},
                "checkpoint", {"step": "authorization", "snapshot_digest": request.snapshot_digest},
            )
            return self.public_view(updated)

    def preflight(self, owner: str, run_id: str, expected_version: int) -> None:
        record = self.store.get(owner, run_id)
        if record["state_version"] != expected_version:
            raise HarnessError("version_conflict")
        if record["state"] != "queued":
            raise HarnessError("not_executable")
        if record["execution_profile"] != "synthetic_test":
            # 授权和计费边界完成前，在线执行始终关闭，不能借工程标记外发。
            if record.get("approval"):
                raise HarnessError("outbound_transport_unavailable", 503)
            raise HarnessError("authorization_required")
        if (self.dependencies.execution_profile != "synthetic_test"
                or record["quality_gate"] != "engineering_only"
                or record["endpoint_profile_digest"] != self.dependencies.endpoint_profile_digest
                or record["policy_digest"] != self.dependencies.policy_digest
                or record["snapshot"]["knowledge_manifest_digest"]
                != self.dependencies.knowledge_manifest_digest):
            raise HarnessError("authorization_stale")

    def claim(self, owner: str, run_id: str, expected_version: int) -> int:
        self.preflight(owner, run_id, expected_version)
        return self.store.acquire(owner, run_id, expected_version, uuid4())

    async def execute(self, owner: str, run_id: str, expected_version: int) -> dict:
        return await self.execute_claimed(owner, run_id, self.claim(owner, run_id, expected_version))

    async def execute_claimed(self, owner: str, run_id: str, token: int) -> dict:
        roles = None
        controller = asyncio.current_task()
        heartbeat_failed = False

        async def heartbeat():
            nonlocal heartbeat_failed
            try:
                while True:
                    await asyncio.sleep(5)
                    await asyncio.to_thread(self.store.heartbeat, owner, run_id, token)
            except Exception:
                heartbeat_failed = True
                logger.warning("报告运行心跳失效，停止当前执行。", extra={"run_id": run_id})
                controller.cancel()

        pulse = asyncio.create_task(heartbeat())
        try:
            self.store.assert_active(owner, run_id, token)
            roles = self.dependencies.roles_factory()
            async with asyncio.timeout(self.dependencies.max_active_seconds):
                record = self.store.get(owner, run_id)
                snapshot = deepcopy(record["snapshot"])
                self.store.assert_active(owner, run_id, token)
                prepared = await roles.prepare(deepcopy(snapshot))
                if set(prepared) != {"guidance", "knowledge"} or prepared["knowledge"]:
                    raise HarnessError("knowledge_tools_unavailable", 503)
                self.store.transition(owner, run_id, token, "generating",
                                      {"prepared": prepared}, "checkpoint", {"step": "prepared"})
                self.store.assert_active(owner, run_id, token)
                candidate = CandidateReport.model_validate(await roles.generate({
                    "snapshot": deepcopy(snapshot), "prepared": deepcopy(prepared),
                    "candidate_version": 1,
                }))
                if candidate.version != 1:
                    raise ValueError("候选版本不匹配。")
                candidate_data = candidate.model_dump(mode="json")
                self.store.transition(
                    owner, run_id, token, "checking",
                    {"candidate": candidate_data, "candidate_version": 1},
                    "checkpoint", {"step": "candidate", "digest": canonical_digest(candidate_data)},
                )
                # 审查上下文重新构造，不传生成者私有历史或自评。
                self.store.assert_active(owner, run_id, token)
                review = ReviewResult.model_validate(await roles.review({
                    "snapshot": deepcopy(snapshot),
                    "snapshot_digest": record["snapshot_digest"],
                    "candidate": deepcopy(candidate_data),
                    "candidate_digest": canonical_digest(candidate_data),
                    "unresolved_issues": [],
                }))
                review_data = review.model_dump(mode="json")
                self.store.transition(owner, run_id, token, "checking", {"review": review_data},
                                      "review", {"candidate_version": 1})
                obligations = snapshot["fact_obligations"]
                validate_publication(
                    candidate, review, record["snapshot_digest"],
                    {item["obligation_id"] for item in obligations},
                    {ref for item in obligations for ref in item["source_refs"]},
                    set(),
                )
                published = self.store.transition(
                    owner, run_id, token, "published", {
                        "review_status": "passed",
                        "report": ReportResult(report_markdown=candidate.report_markdown).model_dump(),
                        "publication": {
                            "candidate_digest": canonical_digest(candidate_data),
                            "review_digest": canonical_digest(review_data),
                            "snapshot_digest": record["snapshot_digest"],
                        },
                    }, "final", {"state": "published"},
                )
                return self.public_view(published)
        except asyncio.CancelledError:
            if not heartbeat_failed:
                self.cancel_claimed(owner, run_id, token)
            raise
        except (ValidationError, ValueError, TimeoutError) as exc:
            reason = "budget_exhausted" if isinstance(exc, TimeoutError) else "invalid_review_or_candidate"
            self._stop_if_owned(owner, run_id, token, "needs_review", reason)
            return self.get(owner, run_id)
        except HarnessError as exc:
            if exc.code != "lease_lost":
                self._stop_if_owned(owner, run_id, token, "failed", exc.code)
            raise
        except Exception:
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

    def _stop_if_owned(self, owner: str, run_id: str, token: int,
                       state: str, reason: str) -> None:
        try:
            self.store.transition(owner, run_id, token, state,
                                  {"terminal_reason": reason, "review_status": "failed"},
                                  "error", {"reason": reason})
        except HarnessError as exc:
            # 取消或租约抢占已经建立更强屏障，不覆盖其终态。
            if exc.code != "lease_lost":
                raise

    def cancel(self, owner: str, run_id: str) -> dict:
        return self.public_view(self.store.cancel(owner, run_id))

    def cancel_claimed(self, owner: str, run_id: str, token: int) -> dict:
        return self.public_view(self.store.cancel(owner, run_id, expected_token=token))
