from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Callable

from pydantic import ValidationError
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.report_harness.contracts import CandidateReport, canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.journal import JOURNAL_VERSION
from app.report_harness.resources import assert_run_capacity
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.role_loop import ToolTurn, normalize_tool_response
from app.report_harness.store import RunStore

_RECOVERABLE_STATES = frozenset({"preparing", "generating", "checking", "revising"})


def can_resume_protocol(document: object, *, unknown_requests: object = None) -> bool:
    """仅恢复首份候选之前已结算的结构故障，不放宽独立审查或事实判断。"""
    if not isinstance(document, dict):
        return False
    if (document.get("state") != "needs_review"
            or document.get("terminal_reason") != "invalid_review_or_candidate"
            or type(document.get("candidate_version")) is not int
            or document["candidate_version"] != 0
            or document.get("candidate") is not None
            or document.get("candidate_history") not in (None, [])):
        return False
    if type(unknown_requests) is not int or unknown_requests != 0:
        return False

    journal = document.get("execution_journal")
    if not isinstance(journal, dict) or journal.get("version") != JOURNAL_VERSION:
        return False
    attempts = journal.get("attempts")
    entries = journal.get("entries")
    if (not isinstance(attempts, dict) or not isinstance(entries, dict)
            or not {"model", "tool"} <= attempts.keys()
            or not attempts.keys() <= {"model", "tool", "prepare"}
            or any(type(value) is not int or value < 0 for value in attempts.values())):
        return False

    model_entries = []
    category_counts = {"model": 0, "tool": 0, "prepare": 0}
    try:
        for key, entry in entries.items():
            if (not isinstance(key, str) or len(key) != 64
                    or any(char not in "0123456789abcdef" for char in key)
                    or not isinstance(entry, dict)
                    or entry.get("category") not in category_counts
                    or type(entry.get("fencing_token")) is not int
                    or entry["fencing_token"] <= 0
                    or entry.get("status") not in {"intent", "committed", "denied"}
                    or canonical_digest({
                        "category": entry["category"], "identity": entry["identity"],
                    }) != key):
                return False
            category_counts[entry["category"]] += 1
            if entry["status"] != "committed":
                return False
            result = entry.get("result")
            if (not isinstance(result, dict)
                    or canonical_digest(result) != entry.get("result_digest")):
                return False
            if entry["category"] == "model":
                model_entries.append(entry)
    except (KeyError, TypeError, ValueError):
        return False
    if any(category_counts[key] > attempts.get(key, 0) for key in category_counts):
        return False
    # JSONB对象不保留插入时序；检查全部回合，不猜测“最后一项”。
    if not model_entries:
        return False
    repairable = False
    for entry in model_entries:
        identity, result = entry["identity"], entry["result"]
        if (not isinstance(identity, dict) or identity.get("role") != "generator"
                or not isinstance(identity.get("context"), dict)
                or type(identity["context"].get("candidate_version")) is not int
                or identity["context"]["candidate_version"] != 1):
            return False
        try:
            CandidateReport.model_validate(result)
        except ValidationError:
            pass
        else:
            # 有完整候选却被后续引用/版本检查拒绝，不属于格式修复。
            return False
        try:
            normalized = normalize_tool_response(result)
            ToolTurn.model_validate(normalized)
            repairable = repairable or normalized != result
        except (ValidationError, TypeError, ValueError):
            repairable = True
    return repairable


def _tool_contract_journal(document: dict) -> tuple[list[dict], int] | None:
    journal = document.get("execution_journal")
    if not isinstance(journal, dict) or journal.get("version") != JOURNAL_VERSION:
        return None
    attempts = journal.get("attempts")
    entries = journal.get("entries")
    if (not isinstance(attempts, dict) or not isinstance(entries, dict)
            or not {"model", "tool"} <= attempts.keys()
            or not attempts.keys() <= {"model", "tool", "prepare"}
            or any(type(value) is not int or value < 0 for value in attempts.values())):
        return None

    category_counts = {"model": 0, "tool": 0, "prepare": 0}
    denied_count = 0
    try:
        for key, entry in entries.items():
            identity = entry.get("identity") if isinstance(entry, dict) else None
            if (not isinstance(key, str) or len(key) != 64
                    or any(char not in "0123456789abcdef" for char in key)
                    or not isinstance(entry, dict)
                    or not isinstance(identity, dict)
                    or entry.get("category") not in category_counts
                    or type(entry.get("fencing_token")) is not int
                    or entry["fencing_token"] <= 0
                    or entry.get("status") not in {"committed", "denied"}
                    or canonical_digest({
                        "category": entry["category"], "identity": identity,
                    }) != key):
                return None
            category_counts[entry["category"]] += 1
            if entry["category"] == "tool" and (
                    identity.get("role") not in {"generator", "reviewer"}
                    or not isinstance(identity.get("call_id"), str)
                    or not isinstance(identity.get("name"), str)
                    or not isinstance(identity.get("arguments"), dict)):
                return None
            if entry["status"] == "denied":
                if (entry["category"] != "tool"
                        or identity.get("name") != "search_knowledge"
                        or entry.get("code") != "retrieval_policy_exceeded"):
                    return None
                denied_count += 1
                continue
            result = entry.get("result")
            if (not isinstance(result, dict)
                    or canonical_digest(result) != entry.get("result_digest")):
                return None
    except (KeyError, TypeError, ValueError):
        return None
    if any(category_counts[key] > attempts.get(key, 0) for key in category_counts):
        return None
    if denied_count == 0:
        return None
    return list(entries.values()), denied_count


def can_resume_tool_contract(document: object, *, unknown_requests: object = None) -> bool:
    """仅恢复已生成候选、审查前因检索策略拒绝而失败的运行。"""
    if not isinstance(document, dict):
        return False
    if (document.get("state") != "failed"
            or document.get("terminal_reason") != "retrieval_policy_exceeded"
            or type(document.get("candidate_version")) is not int
            or document["candidate_version"] != 1
            or document.get("review") is not None
            or document.get("review_history") not in (None, [])):
        return False
    if type(unknown_requests) is not int or unknown_requests != 0:
        return False

    try:
        candidate = CandidateReport.model_validate(document.get("candidate"))
        candidate_data = candidate.model_dump(mode="json")
        candidate_digest = canonical_digest(candidate_data)
        if (candidate.version != 1
                or canonical_digest(document["candidate"]) != candidate_digest):
            return False
    except (KeyError, TypeError, ValueError, ValidationError):
        return False

    history = document.get("candidate_history")
    if (not isinstance(history, list) or len(history) != 1
            or not isinstance(history[0], dict)
            or type(history[0].get("version")) is not int
            or history[0]["version"] != 1):
        return False
    history_item = history[0]
    try:
        if (history_item.get("digest") != candidate_digest
                or canonical_digest(history_item["candidate"]) != candidate_digest):
            return False
    except (KeyError, TypeError, ValueError):
        return False

    journal_data = _tool_contract_journal(document)
    if journal_data is None:
        return False
    entries, _ = journal_data
    candidate_entries = []
    for entry in entries:
        if entry["category"] != "model" or entry["identity"].get("role") != "generator":
            continue
        try:
            normalized = CandidateReport.model_validate(entry["result"])
        except (TypeError, ValueError, ValidationError):
            continue
        if (normalized.version != 1
                or canonical_digest(normalized.model_dump(mode="json")) != candidate_digest):
            return False
        candidate_entries.append(entry)
    if len(candidate_entries) != 1:
        return False
    raw_candidate_digest = candidate_entries[0]["result_digest"]
    repaired_digests = set()
    context = candidate_entries[0]["identity"].get("context")
    if not isinstance(context, dict):
        return False
    feedback = context.get("protocol_feedback", {})
    if not isinstance(feedback, dict):
        return False
    repairs = feedback.get("repairs", [])
    if not isinstance(repairs, list) or len(repairs) > 2:
        return False
    for repair in repairs:
        digest = repair.get("response_digest") if isinstance(repair, dict) else None
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            return False
        repaired_digests.add(digest)
    committed_generator_digests = {
        entry["result_digest"] for entry in entries
        if entry["category"] == "model"
        and entry["identity"].get("role") == "generator"
        and entry["status"] == "committed"
        and entry["result_digest"] != raw_candidate_digest
    }
    if not repaired_digests <= committed_generator_digests:
        return False
    generator_candidates = 0
    for entry in entries:
        if entry["category"] != "model":
            continue
        identity = entry["identity"]
        context = identity.get("context")
        result = entry["result"]
        if (identity.get("role") not in {"generator", "reviewer"}
                or not isinstance(context, dict)):
            return False
        role = identity["role"]
        if role == "generator":
            if (type(context.get("candidate_version")) is not int
                    or context["candidate_version"] != 1):
                return False
            try:
                saved_candidate = CandidateReport.model_validate(result)
            except (TypeError, ValueError, ValidationError):
                # 仅接受完整候选的既有修复反馈实际引用的历史响应，不按案例字段特判。
                if entry["result_digest"] in repaired_digests:
                    continue
                try:
                    ToolTurn.model_validate(normalize_tool_response(result))
                except (TypeError, ValueError, ValidationError):
                    return False
            else:
                if (saved_candidate.version != 1
                        or canonical_digest(saved_candidate.model_dump(mode="json")) != candidate_digest):
                    return False
                generator_candidates += 1
        else:
            if (context.get("candidate_digest") != candidate_digest
                    or canonical_digest(context.get("candidate")) != candidate_digest):
                return False
            try:
                ToolTurn.model_validate(normalize_tool_response(result))
            except (TypeError, ValueError, ValidationError):
                return False
    return generator_candidates == 1


def _active_document_until(row: dict, end_at: datetime, reason: str) -> dict:
    document = deepcopy(row["document"] or {})
    if not isinstance(document, dict):
        raise HarnessError("document_invalid", 422)
    active_seconds = float(document.get("active_seconds", 0) or 0)
    started_at = document.get("active_started_at")
    lease_expires_at = row["lease_expires_at"]
    if started_at and lease_expires_at is not None:
        started = datetime.fromisoformat(str(started_at))
        if started.tzinfo is None and lease_expires_at.tzinfo is not None:
            started = started.replace(tzinfo=lease_expires_at.tzinfo)
        active_seconds += max(0.0, (lease_expires_at - started).total_seconds())
    document.update(
        {
            "active_seconds": active_seconds,
            "active_started_at": None,
            "terminal_reason": reason,
        }
    )
    return document


def _validate_minimum(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise HarnessError("invalid_token_reservation", 422, {"field": field})
    return value


class RunRecovery:
    """提供崩溃后的本地run对账和显式恢复租约，不执行任何外部调用。"""

    def __init__(self, store: RunStore):
        self.store = store

    def sweep_expired(self, limit: int = 100) -> int:
        """有界扫描；真正变更仍在单 run 锁内复核租约及删除屏障。"""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("清理批次必须在 1 到 1000 之间")
        self.store.check_schema()
        with self.store.connection() as conn:
            conn.row_factory = dict_row
            rows = conn.execute(
                "SELECT owner_user_id,run_id FROM report_runs "
                "WHERE state=ANY(%s) AND deleted_at IS NULL "
                "AND lease_expires_at<=clock_timestamp() "
                "ORDER BY lease_expires_at,run_id LIMIT %s",
                (sorted(_RECOVERABLE_STATES), limit),
            ).fetchall()
        for row in rows:
            try:
                self.recover_expired(str(row["owner_user_id"]), str(row["run_id"]))
            except HarnessError as exc:
                if exc.code != "not_found":
                    raise
        return len(rows)

    def recover_expired(self, owner: str, run_id: str) -> dict:
        with self.store.locked(owner, run_id) as (conn, row):
            if row["state"] not in _RECOVERABLE_STATES:
                return self.store._view(row)
            lease_expires_at = row["lease_expires_at"]
            if lease_expires_at is None or lease_expires_at > row["db_now"]:
                return self.store._view(row)

            unknown_rows = conn.execute(
                "SELECT request_id FROM report_run_requests WHERE run_id=%s "
                "AND status IN ('intent','dispatched') ORDER BY request_id",
                (row["run_id"],),
            ).fetchall()
            unknown_request_ids = [str(item["request_id"]) for item in unknown_rows]
            document = _active_document_until(
                row, lease_expires_at, "orphaned_process",
            )
            next_version = row["state_version"] + 1
            next_seq = row["last_event_seq"] + 1
            updated = conn.execute(
                "UPDATE report_runs SET state='suspended',state_version=%s,"
                "document=%s,last_event_seq=%s,fencing_token=fencing_token+1,"
                "lease_owner=NULL,lease_expires_at=NULL "
                "WHERE run_id=%s AND state_version=%s AND deleted_at IS NULL "
                "RETURNING *",
                (
                    next_version,
                    Jsonb(document),
                    next_seq,
                    row["run_id"],
                    row["state_version"],
                ),
            ).fetchone()
            if updated is None:
                raise HarnessError("version_conflict")
            conn.execute(
                "UPDATE report_run_requests SET status='completion_unknown' "
                "WHERE run_id=%s AND status IN ('intent','dispatched')",
                (row["run_id"],),
            )
            conn.execute(
                "INSERT INTO report_run_events(run_id,seq,type,state_version,data) "
                "VALUES (%s,%s,'error',%s,%s)",
                (
                    row["run_id"],
                    next_seq,
                    next_version,
                    Jsonb(
                        {
                            "reason": "orphaned_process",
                            "from_state": row["state"],
                            "unknown_request_ids": unknown_request_ids,
                        }
                    ),
                ),
            )
            return self.store._view({**updated, "db_now": row["db_now"]})

    def resume_claim(
        self,
        owner: str,
        run_id: str,
        expected_version: int,
        worker: Any,
        *,
        retry_unknown_requests: bool,
        validate: Callable[[dict], None],
        minimum_requests: int,
        minimum_tokens: int,
        max_active_runs: int | None = None,
    ) -> int:
        if type(expected_version) is not int or expected_version < 0:
            raise HarnessError("invalid_expected_version", 422)
        if type(retry_unknown_requests) is not bool:
            raise HarnessError("invalid_resume_request", 422)
        minimum_requests = _validate_minimum(minimum_requests, "minimum_requests")
        minimum_tokens = _validate_minimum(minimum_tokens, "minimum_tokens")
        if not callable(validate):
            raise HarnessError("invalid_resume_validator", 422)

        with self.store.locked(
            owner, run_id, expected_version=expected_version,
        ) as (conn, row):
            protocol_resume = can_resume_protocol(
                self.store._view(row), unknown_requests=0,
            )
            tool_contract_resume = can_resume_tool_contract(
                self.store._view(row), unknown_requests=0,
            )
            special_resume = protocol_resume or tool_contract_resume
            if row["state"] != "suspended" and not special_resume:
                raise HarnessError(
                    "not_resumable", 409, {"state": row["state"]},
                )
            if special_resume and retry_unknown_requests:
                raise HarnessError("invalid_resume_request", 422)
            document = deepcopy(row["document"] or {})
            if not isinstance(document, dict):
                raise HarnessError("document_invalid", 422)
            validate(deepcopy(document))
            assert_run_capacity(conn, max_active_runs)

            unknown_rows = conn.execute(
                "SELECT request_id FROM report_run_requests WHERE run_id=%s "
                "AND (status IN ('intent','dispatched','completion_unknown') OR "
                "(status='committed' AND actual_tokens IS NULL)) "
                "ORDER BY request_id",
                (row["run_id"],),
            ).fetchall()
            unknown_request_ids = [str(item["request_id"]) for item in unknown_rows]
            if unknown_request_ids and not retry_unknown_requests:
                raise HarnessError(
                    "completion_unknown",
                    409,
                    {
                        "unknown_request_ids": unknown_request_ids,
                        "retry_unknown_requests_required": True,
                    },
                )

            policy = RequestLedger._load_policy(row, reject_money=True)
            summary = RequestLedger._aggregate(
                conn,
                row["run_id"],
                max_total_tokens=policy.max_total_tokens,
            )
            if summary["usage_exceeded"]:
                raise HarnessError("usage_exceeded", 409)

            requested_requests = summary["capacity_requests"] + minimum_requests
            if requested_requests > policy.max_physical_requests:
                raise HarnessError(
                    "physical_request_budget_exhausted",
                    409,
                    {
                        "requested": requested_requests,
                        "maximum": policy.max_physical_requests,
                    },
                )
            requested_tokens = (
                summary["known_used"]
                + summary["unknown_reserved"]
                + summary["inflight_reserved"]
                + minimum_tokens
            )
            if requested_tokens > policy.max_total_tokens:
                raise HarnessError(
                    "token_budget_exhausted",
                    409,
                    {
                        "requested": requested_tokens,
                        "maximum": policy.max_total_tokens,
                    },
                )

            resume_now = conn.execute(
                "SELECT clock_timestamp() AS db_now",
            ).fetchone()["db_now"]
            next_token = row["fencing_token"] + 1
            document["approved_unknown_request_ids"] = unknown_request_ids
            document["recovery_token"] = next_token
            document["recovery_count"] = document.get("recovery_count", 0) + 1
            document["active_started_at"] = resume_now.isoformat()
            document["terminal_reason"] = None
            document.pop("terminal_detail", None)
            event = {
                "reason": "resume",
                "from_state": row["state"],
                "retry_unknown_requests": retry_unknown_requests,
                "approved_unknown_request_ids": unknown_request_ids,
            }
            if special_resume:
                # 专用恢复操作原子推进终态，不放宽通用状态机的终态转换规则。
                document["review_status"] = "pending"
                event["recovery_kind"] = (
                    "pre_candidate_protocol" if protocol_resume else "tool_contract"
                )
                next_version, next_seq = row["state_version"] + 1, row["last_event_seq"] + 1
                try:
                    updated = conn.execute(
                        "UPDATE report_runs SET state='preparing',state_version=%s,"
                        "document=%s,last_event_seq=%s,fencing_token=%s,lease_owner=%s,"
                        "lease_expires_at=clock_timestamp()+interval '30 seconds' "
                        "WHERE run_id=%s AND state_version=%s AND deleted_at IS NULL RETURNING run_id",
                        (next_version, Jsonb(document), next_seq, next_token, worker,
                         row["run_id"], row["state_version"]),
                    ).fetchone()
                except UniqueViolation as exc:
                    if exc.diag.constraint_name == "report_runs_active_session":
                        raise HarnessError("active_run_conflict") from exc
                    raise
                if updated is None:
                    raise HarnessError("version_conflict")
                conn.execute(
                    "INSERT INTO report_run_events(run_id,seq,type,state_version,data) "
                    "VALUES (%s,%s,'checkpoint',%s,%s)",
                    (row["run_id"], next_seq, next_version, Jsonb(event)),
                )
                return next_token
            conn.execute(
                "UPDATE report_runs SET fencing_token=%s,lease_owner=%s,"
                "lease_expires_at=clock_timestamp()+interval '30 seconds' "
                "WHERE run_id=%s AND state_version=%s AND deleted_at IS NULL",
                (next_token, worker, row["run_id"], row["state_version"]),
            )
            self.store.save(
                conn,
                row,
                "preparing",
                document,
                "checkpoint",
                event,
            )
            return next_token
