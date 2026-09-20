from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any, Callable

from pydantic import ValidationError
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.journal import JOURNAL_VERSION
from app.report_harness.resources import assert_run_capacity
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.role_loop import ToolCall
from app.report_harness.store import RunStore

_RECOVERABLE_STATES = frozenset({"preparing", "generating", "checking", "revising"})


def can_resume_protocol(document: object, *, unknown_requests: object = None) -> bool:
    """只识别已保存的生成者单工具响应故障，不放宽一般复核终态。"""
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
    # JSONB对象不保留插入时序；此兼容恢复仅接受第一份模型响应，不猜测“最后一项”。
    if len(model_entries) != 1:
        return False

    last = model_entries[0]
    identity = last.get("identity")
    result = last.get("result")
    if (not isinstance(identity, dict)
            or identity.get("role") != "generator"
            or not isinstance(identity.get("context"), dict)
            or identity["context"].get("candidate_version") != 1
            or not isinstance(result, dict)
            or set(result) != {"call_id", "name", "arguments"}):
        return False
    try:
        call = ToolCall.model_validate(result)
    except (ValidationError, TypeError, ValueError):
        return False
    return call.model_dump(mode="json") == result


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
            if row["state"] != "suspended" and not protocol_resume:
                raise HarnessError(
                    "not_resumable", 409, {"state": row["state"]},
                )
            if protocol_resume and retry_unknown_requests:
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
            event = {
                "reason": "resume",
                "from_state": row["state"],
                "retry_unknown_requests": retry_unknown_requests,
                "approved_unknown_request_ids": unknown_request_ids,
            }
            if protocol_resume:
                # 专用恢复操作原子推进终态，不放宽通用状态机的终态转换规则。
                document["review_status"] = "pending"
                event["recovery_kind"] = "single_tool_envelope"
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
