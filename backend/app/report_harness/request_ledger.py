from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.schemas.report_run import BudgetPolicy


_ROLES = frozenset({"expert", "generator", "reviewer", "embedding", "probe"})
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_PHYSICAL_STATUSES = ("dispatched", "committed", "completion_unknown")
_CAPACITY_STATUSES = ("intent", "dispatched", "committed", "completion_unknown")


class RequestLedger:
    """在现有 PostgreSQL 请求表上提供带租约围栏的物理请求账本。"""

    def __init__(self, store: Any) -> None:
        self.store = store

    def ensure_capacity(
        self,
        owner: str,
        run_id: str,
        token: int,
        *,
        requests: int,
        tokens: int,
    ) -> None:
        """在当前租约下预检物理请求槽位和 token 预算。"""
        self._validate_token(token)
        self._validate_tokens(requests, "requests")
        self._validate_tokens(tokens, "tokens")

        with self.store.locked(owner, run_id, fencing_token=token) as (conn, row):
            policy = self._load_policy(row, reject_money=True)
            summary = self._aggregate(
                conn,
                row["run_id"],
                max_total_tokens=policy.max_total_tokens,
            )
            self._reject_blocked(summary)

            requested_capacity = summary["capacity_requests"] + requests
            if requested_capacity > policy.max_physical_requests:
                raise HarnessError(
                    "physical_request_budget_exhausted",
                    409,
                    {
                        "requested": requested_capacity,
                        "maximum": policy.max_physical_requests,
                    },
                )

            requested_tokens = (
                summary["known_used"]
                + summary["unknown_reserved"]
                + summary["inflight_reserved"]
                + tokens
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

    def reserve(
        self,
        owner: str,
        run_id: str,
        token: int,
        *,
        role: str,
        endpoint_digest: str,
        request_digest: str,
        reserved_tokens: int,
        review_reserve_tokens: int,
        generation_reserve_tokens: int = 0,
    ) -> dict:
        self._validate_token(token)
        self._validate_role(role)
        self._validate_request_digest(request_digest)
        self._validate_tokens(reserved_tokens, "reserved_tokens")
        self._validate_tokens(review_reserve_tokens, "review_reserve_tokens")
        self._validate_tokens(generation_reserve_tokens, "generation_reserve_tokens")

        with self.store.locked(owner, run_id, fencing_token=token) as (conn, row):
            policy = self._load_policy(row, reject_money=True)
            expected_endpoint = row["document"].get("endpoint_profile_digest")
            if not isinstance(expected_endpoint, str) or endpoint_digest != expected_endpoint:
                raise HarnessError(
                    "endpoint_digest_conflict",
                    409,
                    {"expected": expected_endpoint, "actual": endpoint_digest},
                )

            summary = self._aggregate(
                conn,
                row["run_id"],
                max_total_tokens=policy.max_total_tokens,
            )
            self._reject_blocked(summary)

            preparing_generation = row["state"] == "preparing" and role in {
                "expert",
                "embedding",
                "probe",
            }
            if preparing_generation:
                extra_request = 2
            else:
                extra_request = 0 if role == "reviewer" else 1
            capacity_requests = summary["capacity_requests"] + 1 + extra_request
            if capacity_requests > policy.max_physical_requests:
                raise HarnessError(
                    "physical_request_budget_exhausted",
                    409,
                    {
                        "requested": capacity_requests,
                        "maximum": policy.max_physical_requests,
                    },
                )

            retrieval_capacity = summary["capacity_retrieval_requests"] + (
                1 if role == "embedding" else 0
            )
            if retrieval_capacity > policy.max_retrieval_requests:
                raise HarnessError(
                    "retrieval_request_budget_exhausted",
                    409,
                    {
                        "requested": retrieval_capacity,
                        "maximum": policy.max_retrieval_requests,
                    },
                )

            if preparing_generation:
                future_token_reserve = (
                    generation_reserve_tokens + review_reserve_tokens
                )
            else:
                future_token_reserve = (
                    review_reserve_tokens if role != "reviewer" else 0
                )
            token_capacity = (
                summary["known_used"]
                + summary["unknown_reserved"]
                + summary["inflight_reserved"]
                + reserved_tokens
                + future_token_reserve
            )
            if token_capacity > policy.max_total_tokens:
                raise HarnessError(
                    "token_budget_exhausted",
                    409,
                    {
                        "requested": token_capacity,
                        "maximum": policy.max_total_tokens,
                    },
                )

            request_id = uuid4()
            attempt_id = uuid4()
            request_row = conn.execute(
                "INSERT INTO report_run_requests "
                "(request_id,run_id,attempt_id,fencing_token,role,endpoint_digest,"
                "request_digest,status,reserved_tokens) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'intent',%s) RETURNING *",
                (
                    request_id,
                    row["run_id"],
                    attempt_id,
                    token,
                    role,
                    endpoint_digest,
                    request_digest,
                    reserved_tokens,
                ),
            ).fetchone()
            self.store.save(
                conn,
                row,
                row["state"],
                row["document"],
                "request",
                {
                    "request_id": str(request_id),
                    "attempt_id": str(attempt_id),
                    "role": role,
                    "status": "intent",
                    "reserved_tokens": reserved_tokens,
                },
            )
            return self._request_view(request_row)

    def dispatch(
        self,
        owner: str,
        run_id: str,
        token: int,
        request_id: str,
        attempt_id: str,
    ) -> dict:
        self._validate_token(token)
        request_uuid = self._request_uuid(request_id, "request_id")
        attempt_uuid = self._request_uuid(attempt_id, "attempt_id")

        with self.store.locked(owner, run_id, fencing_token=token) as (conn, row):
            policy = self._load_policy(row, reject_money=True)
            self._reject_blocked(self._aggregate(
                conn, row["run_id"], max_total_tokens=policy.max_total_tokens,
            ))
            request_row = self._lock_request(
                conn, row["run_id"], request_uuid, attempt_uuid, token,
            )
            if request_row["status"] != "intent":
                raise HarnessError(
                    "request_state_conflict",
                    409,
                    {"expected": "intent", "actual": request_row["status"]},
                )
            updated = conn.execute(
                "UPDATE report_run_requests SET status='dispatched' "
                "WHERE request_id=%s AND run_id=%s AND attempt_id=%s "
                "AND fencing_token=%s AND status='intent' RETURNING *",
                (request_uuid, row["run_id"], attempt_uuid, token),
            ).fetchone()
            if updated is None:
                raise HarnessError("request_state_conflict", 409)
            self.store.save(
                conn,
                row,
                row["state"],
                row["document"],
                "request",
                {
                    "request_id": str(updated["request_id"]),
                    "attempt_id": str(updated["attempt_id"]),
                    "role": updated["role"],
                    "status": "dispatched",
                    "reserved_tokens": updated["reserved_tokens"],
                },
            )
            return self._request_view(updated)

    def settle(
        self,
        owner: str,
        run_id: str,
        token: int,
        request_id: str,
        attempt_id: str,
        *,
        actual_tokens: int | None,
        result: dict,
    ) -> dict:
        self._validate_token(token)
        self._validate_actual_tokens(actual_tokens)
        result_copy = self._validate_result(result)
        request_uuid = self._request_uuid(request_id, "request_id")
        attempt_uuid = self._request_uuid(attempt_id, "attempt_id")

        # 不带当前 fencing_token 锁 run，才能让已登记的旧 attempt 在终态迟到结算。
        settlement_conflict = False
        settled_view: dict | None = None
        with self.store.locked_settlement(owner, run_id) as (conn, row):
            request_row = self._lock_request(
                conn, row["run_id"], request_uuid, attempt_uuid, token,
            )
            status = request_row["status"]
            if status in {"intent", "rejected"}:
                raise HarnessError(
                    "request_state_conflict",
                    409,
                    {"expected": "dispatched_or_unknown", "actual": status},
                )

            if status == "committed":
                if request_row["result"] != result_copy:
                    self._append_settlement_conflict_audit(
                        conn,
                        row,
                        request_row,
                        actual_tokens=actual_tokens,
                        result=result_copy,
                        reason="result_mismatch",
                    )
                    settlement_conflict = True
                else:
                    stored_actual = request_row["actual_tokens"]
                    if stored_actual is None:
                        if actual_tokens is None:
                            settled_view = self._request_view(request_row)
                        else:
                            updated = conn.execute(
                                "UPDATE report_run_requests SET actual_tokens=%s,"
                                "settled_at=clock_timestamp() WHERE request_id=%s "
                                "AND run_id=%s AND attempt_id=%s AND fencing_token=%s "
                                "AND status='committed' AND actual_tokens IS NULL RETURNING *",
                                (
                                    actual_tokens,
                                    request_uuid,
                                    row["run_id"],
                                    attempt_uuid,
                                    token,
                                ),
                            ).fetchone()
                            if updated is None:
                                self._append_settlement_conflict_audit(
                                    conn,
                                    row,
                                    request_row,
                                    actual_tokens=actual_tokens,
                                    result=result_copy,
                                    reason="usage_update_conflict",
                                )
                                settlement_conflict = True
                            else:
                                settled_view = self._request_view(updated)
                    elif actual_tokens != stored_actual:
                        self._append_settlement_conflict_audit(
                            conn,
                            row,
                            request_row,
                            actual_tokens=actual_tokens,
                            result=result_copy,
                            reason="actual_tokens_mismatch",
                        )
                        settlement_conflict = True
                    else:
                        settled_view = self._request_view(request_row)

            elif status == "completion_unknown" and request_row["result"] is not None:
                if request_row["result"] != result_copy:
                    self._append_settlement_conflict_audit(
                        conn,
                        row,
                        request_row,
                        actual_tokens=actual_tokens,
                        result=result_copy,
                        reason="result_mismatch",
                    )
                    settlement_conflict = True

            if not settlement_conflict and settled_view is None:
                if status not in {"dispatched", "completion_unknown"}:
                    raise HarnessError("request_state_conflict", 409, {"actual": status})

                updated = conn.execute(
                    "UPDATE report_run_requests SET status='committed',actual_tokens=%s,"
                    "result=%s,settled_at=clock_timestamp() WHERE request_id=%s "
                    "AND run_id=%s AND attempt_id=%s AND fencing_token=%s "
                    "AND status IN ('dispatched','completion_unknown') RETURNING *",
                    (
                        actual_tokens,
                        Jsonb(result_copy),
                        request_uuid,
                        row["run_id"],
                        attempt_uuid,
                        token,
                    ),
                ).fetchone()
                if updated is None:
                    self._append_settlement_conflict_audit(
                        conn,
                        row,
                        request_row,
                        actual_tokens=actual_tokens,
                        result=result_copy,
                        reason="settlement_update_conflict",
                    )
                    settlement_conflict = True
                else:
                    settled_view = self._request_view(updated)

        if settlement_conflict:
            raise HarnessError("settlement_conflict", 409)
        if settled_view is None:
            raise HarnessError("settlement_conflict", 409)
        return settled_view

    def mark_unknown(
        self,
        owner: str,
        run_id: str,
        token: int,
        request_id: str,
        attempt_id: str,
    ) -> None:
        self._validate_token(token)
        request_uuid = self._request_uuid(request_id, "request_id")
        attempt_uuid = self._request_uuid(attempt_id, "attempt_id")
        with self.store.locked_settlement(owner, run_id) as (conn, row):
            request_row = self._lock_request(
                conn, row["run_id"], request_uuid, attempt_uuid, token,
            )
            if request_row["status"] == "completion_unknown":
                return
            if request_row["status"] != "dispatched":
                raise HarnessError(
                    "request_state_conflict",
                    409,
                    {"expected": "dispatched", "actual": request_row["status"]},
                )
            updated = conn.execute(
                "UPDATE report_run_requests SET status='completion_unknown' "
                "WHERE request_id=%s AND run_id=%s AND attempt_id=%s "
                "AND fencing_token=%s AND status='dispatched' RETURNING request_id",
                (request_uuid, row["run_id"], attempt_uuid, token),
            ).fetchone()
            if updated is None:
                raise HarnessError("request_state_conflict", 409)

    def reject_unsent(
        self,
        owner: str,
        run_id: str,
        token: int,
        request_id: str,
        attempt_id: str,
    ) -> None:
        self._validate_token(token)
        request_uuid = self._request_uuid(request_id, "request_id")
        attempt_uuid = self._request_uuid(attempt_id, "attempt_id")
        with self.store.locked_settlement(owner, run_id) as (conn, row):
            request_row = self._lock_request(
                conn, row["run_id"], request_uuid, attempt_uuid, token,
            )
            if request_row["status"] == "rejected":
                return
            if request_row["status"] != "intent":
                raise HarnessError(
                    "request_state_conflict",
                    409,
                    {"expected": "intent", "actual": request_row["status"]},
                )
            updated = conn.execute(
                "UPDATE report_run_requests SET status='rejected' "
                "WHERE request_id=%s AND run_id=%s AND attempt_id=%s "
                "AND fencing_token=%s AND status='intent' RETURNING request_id",
                (request_uuid, row["run_id"], attempt_uuid, token),
            ).fetchone()
            if updated is None:
                raise HarnessError("request_state_conflict", 409)

    def policy(self, owner: str, run_id: str, token: int) -> BudgetPolicy:
        with self.store.locked(owner, run_id, fencing_token=token) as (_, row):
            return self._load_policy(row, reject_money=True)

    def view(self, owner: str, run_id: str) -> dict:
        with self.store.locked(owner, run_id) as (conn, row):
            policy = self._load_policy(row, reject_money=False)
            summary = self._aggregate(
                conn,
                row["run_id"],
                max_total_tokens=policy.max_total_tokens,
            )
            return {
                "physical_requests": summary["physical_requests"],
                "known_used": summary["known_used"],
                "unknown_reserved": summary["unknown_reserved"],
                "inflight_reserved": summary["inflight_reserved"],
                "remaining": summary["remaining"],
                "unknown_requests": summary["unknown_requests"],
                "usage_exceeded": summary["usage_exceeded"],
                "retrieval_requests": summary["retrieval_requests"],
            }

    @staticmethod
    def _validate_token(token: object) -> None:
        if type(token) is not int or token < 0:
            raise HarnessError("invalid_fencing_token", 422)

    @staticmethod
    def _validate_role(role: object) -> None:
        if not isinstance(role, str) or role not in _ROLES:
            raise HarnessError("invalid_request_role", 422)

    @staticmethod
    def _validate_request_digest(request_digest: object) -> None:
        if not isinstance(request_digest, str) or _DIGEST_RE.fullmatch(request_digest) is None:
            raise HarnessError("invalid_request_digest", 422)

    @staticmethod
    def _validate_tokens(value: object, field: str) -> None:
        if type(value) is not int or value < 0:
            raise HarnessError("invalid_token_reservation", 422, {"field": field})

    @staticmethod
    def _validate_actual_tokens(value: object) -> None:
        if value is not None and (type(value) is not int or value < 0):
            raise HarnessError("invalid_actual_tokens", 422)

    @staticmethod
    def _validate_result(result: object) -> dict:
        if not isinstance(result, dict):
            raise HarnessError("invalid_request_result", 422)
        try:
            json.dumps(result, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise HarnessError("invalid_request_result", 422) from exc
        return deepcopy(result)

    @staticmethod
    def _request_uuid(value: object, field: str) -> UUID:
        try:
            return UUID(str(value))
        except (AttributeError, TypeError, ValueError) as exc:
            raise HarnessError("invalid_request_reference", 422, {"field": field}) from exc

    @staticmethod
    def _load_policy(row: dict, *, reject_money: bool = False) -> BudgetPolicy:
        document = row.get("document")
        raw = document.get("budget_policy") if isinstance(document, dict) else None
        if raw is None:
            raise HarnessError("budget_policy_missing", 409)
        try:
            policy = BudgetPolicy.model_validate(raw)
        except (ValidationError, TypeError, ValueError) as exc:
            raise HarnessError("budget_policy_invalid", 422) from exc
        if reject_money and policy.max_money is not None:
            raise HarnessError("money_budget_unverifiable", 409)
        return policy

    @staticmethod
    def _reject_blocked(summary: dict[str, Any]) -> None:
        if summary["unknown_requests"]:
            raise HarnessError("usage_unknown", 409)
        if summary["usage_exceeded"]:
            raise HarnessError("usage_exceeded", 409)

    @staticmethod
    def _aggregate(
        conn: Any,
        run_id: object,
        *,
        max_total_tokens: int | None = None,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT "
            "COUNT(*) FILTER (WHERE status IN ('dispatched','committed',"
            "'completion_unknown')) AS physical_requests, "
            "COUNT(*) FILTER (WHERE status IN ('intent','dispatched','committed',"
            "'completion_unknown')) AS capacity_requests, "
            "COALESCE(SUM(actual_tokens) FILTER (WHERE status='committed' "
            "AND actual_tokens IS NOT NULL), 0) AS known_used, "
            "COALESCE(SUM(reserved_tokens) FILTER (WHERE status='completion_unknown' "
            "OR (status='committed' AND actual_tokens IS NULL)), 0) AS unknown_reserved, "
            "COALESCE(SUM(reserved_tokens) FILTER (WHERE status IN ('intent','dispatched') "
            "AND actual_tokens IS NULL), 0) AS inflight_reserved, "
            "COUNT(*) FILTER (WHERE status='completion_unknown' "
            "OR (status='committed' AND actual_tokens IS NULL)) AS unknown_requests, "
            "COALESCE(BOOL_OR(status='committed' AND actual_tokens > reserved_tokens), FALSE) "
            "AS usage_exceeded, "
            "COUNT(*) FILTER (WHERE role='embedding' AND status IN "
            "('dispatched','committed','completion_unknown')) AS retrieval_requests, "
            "COUNT(*) FILTER (WHERE role='embedding' AND status IN "
            "('intent','dispatched','committed','completion_unknown')) "
            "AS capacity_retrieval_requests "
            "FROM report_run_requests WHERE run_id=%s",
            (run_id,),
        ).fetchone()
        result = {key: row[key] for key in row}
        result["remaining"] = (
            max_total_tokens
            - result["known_used"]
            - result["unknown_reserved"]
            - result["inflight_reserved"]
            if max_total_tokens is not None
            else None
        )
        return result

    @staticmethod
    def _append_settlement_conflict_audit(
        conn: Any,
        run_row: dict,
        request_row: dict,
        *,
        actual_tokens: int | None,
        result: dict,
        reason: str,
    ) -> None:
        """记录不覆盖结算的安全冲突元数据，并保持运行状态与版本不变。"""
        next_seq = run_row["last_event_seq"] + 1
        stored_result = request_row["result"]
        data = {
            "code": "settlement_conflict",
            "reason": reason,
            "request_id": str(request_row["request_id"]),
            "attempt_id": str(request_row["attempt_id"]),
            "role": request_row["role"],
            "status": request_row["status"],
            "reserved_tokens": request_row["reserved_tokens"],
            "stored_actual_tokens": request_row["actual_tokens"],
            "submitted_actual_tokens": actual_tokens,
            "stored_result_digest": (
                canonical_digest(stored_result) if stored_result is not None else None
            ),
            "submitted_result_digest": canonical_digest(result),
        }
        conn.execute(
            "INSERT INTO report_run_events"
            "(run_id,seq,type,state_version,data) VALUES (%s,%s,'error',%s,%s)",
            (
                run_row["run_id"],
                next_seq,
                run_row["state_version"],
                Jsonb(data),
            ),
        )
        updated = conn.execute(
            "UPDATE report_runs SET last_event_seq=%s "
            "WHERE run_id=%s AND last_event_seq=%s RETURNING run_id",
            (next_seq, run_row["run_id"], run_row["last_event_seq"]),
        ).fetchone()
        if updated is None:
            raise HarnessError("version_conflict")

    @staticmethod
    def _request_view(row: dict) -> dict:
        return {
            "request_id": str(row["request_id"]),
            "attempt_id": str(row["attempt_id"]),
            "run_id": str(row["run_id"]),
            "fencing_token": row["fencing_token"],
            "role": row["role"],
            "endpoint_digest": row["endpoint_digest"],
            "request_digest": row["request_digest"],
            "status": row["status"],
            "reserved_tokens": row["reserved_tokens"],
            "actual_tokens": row["actual_tokens"],
            "result": deepcopy(row["result"]),
        }

    @staticmethod
    def _lock_request(
        conn: Any,
        run_id: object,
        request_id: UUID,
        attempt_id: UUID,
        fencing_token: int,
    ) -> dict:
        row = conn.execute(
            "SELECT * FROM report_run_requests WHERE request_id=%s AND run_id=%s "
            "AND attempt_id=%s AND fencing_token=%s FOR UPDATE",
            (request_id, run_id, attempt_id, fencing_token),
        ).fetchone()
        if row is None:
            raise HarnessError("request_not_found", 404)
        return row
