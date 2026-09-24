from __future__ import annotations

from copy import deepcopy
from typing import Awaitable, Callable

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.money_guard import MoneyGuardNotSent


JOURNAL_VERSION = 1


class ExecutionJournal:
    """持久步骤日志只复用完整结果，不把未知请求伪装成已完成步骤。"""

    def __init__(self, store, owner: str, run_id: str, token: int):
        self.store, self.owner, self.run_id, self.token = store, owner, run_id, token
        record = store.get(owner, run_id)
        journal = record.get("execution_journal", {
            "version": JOURNAL_VERSION, "entries": {}, "attempts": {"model": 0, "tool": 0},
        })
        if (not isinstance(journal, dict) or type(journal.get("version")) is not int
                or journal["version"] != JOURNAL_VERSION):
            raise HarnessError("checkpoint_version_mismatch")
        try:
            attempts, entries = journal["attempts"], journal["entries"]
            if (not isinstance(attempts, dict) or not isinstance(entries, dict)
                    or not {"model", "tool"} <= attempts.keys()
                    or not attempts.keys() <= {"model", "tool", "prepare"}
                    or any(type(value) is not int or value < 0 for value in attempts.values())):
                raise ValueError()
            for key, entry in entries.items():
                if (not isinstance(key, str) or len(key) != 64
                        or any(char not in "0123456789abcdef" for char in key)
                        or not isinstance(entry, dict)
                        or entry.get("category") not in {"model", "tool", "prepare"}
                        or type(entry.get("fencing_token")) is not int
                        or entry["fencing_token"] <= 0
                        or entry.get("status") not in {"intent", "committed", "denied"}):
                    raise ValueError()
                if canonical_digest({
                    "category": entry["category"], "identity": entry["identity"],
                }) != key:
                    raise ValueError()
                if entry["status"] == "committed":
                    if (not isinstance(entry["result"], dict)
                            or canonical_digest(entry["result"]) != entry["result_digest"]):
                        raise ValueError()
            for category in ("model", "tool", "prepare"):
                if sum(entry["category"] == category for entry in entries.values()) > attempts.get(category, 0):
                    raise ValueError()
        except (KeyError, TypeError, ValueError) as exc:
            raise HarnessError("checkpoint_invalid") from exc
        self._allow_incomplete_retry = (
            type(record.get("recovery_token")) is int and record["recovery_token"] == token
        )
        self._journal = deepcopy(journal)

    def attempts(self, category: str) -> int:
        return self._journal["attempts"].get(category, 0)

    def model_context(self, role: str, context: dict) -> dict:
        """仅工具说明可升级；已保存回合其余输入必须逐项等价才能复用。"""
        fingerprint = canonical_digest({key: value for key, value in context.items() if key != "tools"})
        matches = []
        for entry in self._journal["entries"].values():
            identity = entry["identity"]
            if entry["category"] != "model" or identity.get("role") != role:
                continue
            saved = identity.get("context")
            if not isinstance(saved, dict):
                continue
            if canonical_digest({key: value for key, value in saved.items() if key != "tools"}) == fingerprint:
                matches.append(saved)
        exact = [item for item in matches if canonical_digest(item) == canonical_digest(context)]
        if exact:
            return deepcopy(exact[0])
        if len(matches) > 1:
            raise HarnessError("checkpoint_context_ambiguous")
        return deepcopy(matches[0] if matches else context)

    def _persist(self, key: str, category: str, status: str, identity: dict) -> None:
        record = self.store.get(self.owner, self.run_id)
        patch = {"execution_journal": deepcopy(self._journal)}
        public = {"step": "execution_journal", "kind": category,
                  "step_digest": key, "status": status}
        if self._journal["entries"][key].get("recovered"):
            public["reused"] = True
        event_type = "checkpoint"
        if category == "tool":
            name = identity["name"]
            public = {
                "id": key, "role": identity["role"],
                "name": name if name in {
                    "list_evidence", "read_evidence", "search_knowledge", "read_knowledge",
                } else "unknown",
                "call_id_digest": canonical_digest(identity["call_id"]),
                "arguments_digest": canonical_digest(identity["arguments"]),
                "status": "completed" if status == "committed" else status,
            }
            private = {
                "tool_calls": self.attempts("tool"), "model_turns": self.attempts("model"),
                "call_id": identity["call_id"], "name": name,
                "arguments": deepcopy(identity["arguments"]),
            }
            if status == "committed":
                saved = self._journal["entries"][key]["result"]
                result = saved["result"]
                private["result"] = deepcopy(result)
                public["result_digest"] = canonical_digest(result)
                patch["knowledge_registry"] = list(saved["tool_state"]["registry"].values())
            if status == "denied":
                public["code"] = self._journal["entries"][key]["code"]
            patch["tool_history"] = [
                *record.get("tool_history", []), {**public, "private": private},
            ]
            event_type = "tool"
        self.store.transition(
            self.owner, self.run_id, self.token, record["state"],
            patch, event_type, public,
        )

    async def invoke(
        self, category: str, identity: dict,
        operation: Callable[[], Awaitable[dict]], *, limit: int | None = None,
        replay_operation: Callable[[], Awaitable[dict | None]] | None = None,
    ) -> dict:
        self.store.assert_active(self.owner, self.run_id, self.token)
        if category not in {"model", "tool", "prepare"}:
            raise HarnessError("invalid_step_category")
        key = canonical_digest({"category": category, "identity": identity})
        entry = self._journal["entries"].get(key)
        if entry and entry["status"] == "committed":
            result = entry["result"]
            if canonical_digest(result) != entry["result_digest"]:
                raise HarnessError("checkpoint_digest_mismatch")
            return deepcopy(result)
        if (entry and entry["status"] == "denied" and category == "tool"
                and identity.get("name") == "search_knowledge"
                and entry.get("code") == "retrieval_policy_exceeded"):
            # 该拒绝发生在检索前，无外部副作用；保留原拒绝，不重复执行或覆盖历史。
            raise HarnessError("retrieval_policy_exceeded")
        if (entry and entry["status"] == "intent" and category != "tool"
                and (not self._allow_incomplete_retry or entry["fencing_token"] == self.token)):
            raise HarnessError("completion_unknown")
        metadata = {
            "category": category, "fencing_token": self.token, "identity": deepcopy(identity),
        }
        if entry and entry["status"] == "intent" and replay_operation is not None:
            replayed = await replay_operation()
            if replayed is not None:
                return self._commit(key, category, identity, replayed, {**metadata, "recovered": True})
        count = self.attempts(category)
        if limit is not None and count >= limit:
            code = "tool_budget_exhausted" if category == "tool" else "model_turn_budget_exhausted"
            raise HarnessError(code)
        self._journal["attempts"][category] = count + 1
        self._journal["entries"][key] = {
            **metadata, "status": "intent",
        }
        self._persist(key, category, "intent", identity)
        try:
            result = await operation()
        except HarnessError as exc:
            if category == "tool" or isinstance(exc, MoneyGuardNotSent):
                self._journal["entries"][key] = {
                    **metadata, "status": "denied", "code": exc.code,
                }
                self._persist(key, category, "denied", identity)
            raise
        return self._commit(key, category, identity, result, metadata)

    def _commit(self, key, category, identity, result, metadata) -> dict:
        if not isinstance(result, dict):
            raise HarnessError("invalid_step_result")
        digest = canonical_digest(result)
        self._journal["entries"][key] = {
            **metadata, "status": "committed",
            "result": deepcopy(result), "result_digest": digest,
        }
        self._persist(key, category, "committed", identity)
        return deepcopy(result)
