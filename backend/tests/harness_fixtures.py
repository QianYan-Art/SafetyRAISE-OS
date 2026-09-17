from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.execution import ReportExecutionDependencies
from app.report_harness.store import TERMINAL_STATES


class SyntheticRoles:
    """固定测试响应，无网络客户端；不承担真实语义判断。"""

    def __init__(self, invalid_review=False, fail_generate=False):
        self.calls = []
        self.closed = False
        self.invalid_review = invalid_review
        self.fail_generate = fail_generate
        self.review_context = None

    async def prepare(self, snapshot):
        self.calls.append("prepare")
        return {"guidance": {"note": "仅用于合成工程样本"}, "knowledge": []}

    async def generate(self, context):
        self.calls.append("generate")
        if self.fail_generate:
            raise RuntimeError("合成生成故障")
        obligations = context["snapshot"]["fact_obligations"]
        text = "合成事故仅按给定事实描述，不作超出材料的责任认定。"
        return {
            "version": context["candidate_version"], "report_markdown": text,
            "claims": [{
                "claim_id": "claim-1", "text_span": {"start": 0, "end": len(text)},
                "type": "fact", "evidence_refs": [
                    ref for item in obligations for ref in item["source_refs"]
                ], "knowledge_refs": [],
            }],
            "obligation_resolutions": [{
                "obligation_id": item["obligation_id"], "treatment": "uncertain",
                "resolution": "合成样本记录该义务的处置，不证明实际语义成立。",
            } for item in obligations],
        }

    async def review(self, context):
        self.calls.append("review")
        self.review_context = deepcopy(context)
        obligations = context["snapshot"]["fact_obligations"]
        result = {
            "candidate_digest": context["candidate_digest"],
            "snapshot_digest": context["snapshot_digest"],
            "coverage_checks": [{
                "obligation_id": item["obligation_id"], "passed": True,
                "claim_ids": ["claim-1"], "evidence_refs": item["source_refs"],
                "knowledge_refs": [], "conclusion": "固定合成覆盖检查结果。",
            } for item in obligations],
            "issues": [],
            "completed_checks": [{
                "category": category, "passed": True,
                "evidence_refs": obligations[0]["source_refs"], "knowledge_refs": [],
                "conclusion": "固定合成检查结果，仅验证控制逻辑。",
            } for category in ["facts", "coverage", "reasoning", "citations", "conciseness"]],
        }
        if self.invalid_review:
            result["candidate_digest"] = "0" * 64
        return result

    async def close(self):
        self.closed = True


def dependencies(roles, profile="synthetic_test"):
    return ReportExecutionDependencies(
        roles_factory=lambda: roles, execution_profile=profile,
        endpoint_profile_digest=canonical_digest({"profile": profile}),
        policy_digest=canonical_digest({"policy": "synthetic-v1"}),
        knowledge_manifest_digest=canonical_digest({"knowledge": []}),
    )


class MemoryStore:
    """只用于控制器单元测试，不证明事务、租约计时或跨进程安全。"""

    def __init__(self):
        self.records, self.owners, self.tokens, self.event_log = {}, {}, {}, {}

    def check_schema(self):
        return None

    def create(self, owner, request_id, request_digest, document):
        identifier = document["run_id"]
        self.records[identifier] = {
            **deepcopy(document), "state": "queued", "state_version": 0, "last_event_seq": 0,
        }
        self.owners[identifier] = owner
        self.tokens[identifier] = 0
        self.event_log[identifier] = []
        return self.get(owner, identifier)

    def get(self, owner, run_id):
        if self.owners.get(run_id) != owner:
            raise HarnessError("not_found", 404)
        return deepcopy(self.records[run_id])

    def acquire(self, owner, run_id, version, worker):
        record = self.get(owner, run_id)
        if record["state_version"] != version or record["state"] != "queued":
            raise HarnessError("version_conflict")
        self.tokens[run_id] += 1
        self.transition(owner, run_id, self.tokens[run_id], "preparing", {})
        return self.tokens[run_id]

    def heartbeat(self, owner, run_id, token):
        if self.tokens[run_id] != token:
            raise HarnessError("lease_lost")

    def assert_active(self, owner, run_id, token):
        record = self.get(owner, run_id)
        if self.tokens[run_id] != token or record["state"] in TERMINAL_STATES:
            raise HarnessError("lease_lost")

    def transition(self, owner, run_id, token, state, patch, event_type="stage", data=None):
        record = self.get(owner, run_id)
        if token != self.tokens[run_id] or record["state"] in TERMINAL_STATES:
            raise HarnessError("lease_lost")
        record.update(deepcopy(patch))
        record.update(state=state, state_version=record["state_version"] + 1,
                      last_event_seq=record["last_event_seq"] + 1)
        self.records[run_id] = record
        self.event_log[run_id].append({
            "run_id": run_id, "seq": record["last_event_seq"], "type": event_type,
            "state_version": record["state_version"], "occurred_at": "2026-01-01T00:00:00Z",
            "data": data or {"stage": state},
        })
        return self.get(owner, run_id)

    def cancel(self, owner, run_id, expected_token=None):
        record = self.get(owner, run_id)
        if expected_token is not None and expected_token != self.tokens[run_id]:
            return record
        if record["state"] in TERMINAL_STATES:
            return record
        result = self.transition(owner, run_id, self.tokens[run_id], "cancelled",
                                 {"terminal_reason": "user_cancelled"})
        self.tokens[run_id] += 1
        return result

    def events(self, owner, run_id, after_seq=0, limit=100):
        self.get(owner, run_id)
        events = [e for e in self.event_log[run_id] if e["seq"] > after_seq][:limit]
        return {"events": deepcopy(events), "next_seq": events[-1]["seq"] if events else after_seq}
