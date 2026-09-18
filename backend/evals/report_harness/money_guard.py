from __future__ import annotations

import inspect
import json
import math
import os
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation, ROUND_CEILING, localcontext
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.report_harness.errors import HarnessError


MICRO_CNY = 1_000_000
BUDGET_CNY = Decimal("100")
BUDGET_MICRO_CNY = 100 * MICRO_CNY
ALLOWED_ROLES = ("generator", "reviewer")
ALLOWED_MODEL = "tencent/hy4-preview"
DEFAULT_EXPERIMENT_ID = "report-line-10-isolated"


class MoneyGuardError(HarnessError):
    """货币账本拒绝或阻断一次 attempt。"""


class MoneyGuardConfigurationError(HarnessError, ValueError):
    """固定实验配置与账本不一致，或本地配置本身无效。"""

    def __init__(self, code: str):
        HarnessError.__init__(self, code)


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS money_guard_experiments (
        experiment_id TEXT PRIMARY KEY,
        budget_micro INTEGER NOT NULL,
        cost_upper_micro INTEGER NOT NULL,
        usd_to_cny_upper TEXT NOT NULL,
        model_digest TEXT NOT NULL,
        endpoint_digest TEXT NOT NULL,
        price_digest TEXT NOT NULL,
        blocked_reason TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS money_guard_attempts (
        attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
        experiment_id TEXT NOT NULL
            REFERENCES money_guard_experiments(experiment_id),
        role TEXT NOT NULL,
        reserved_micro INTEGER NOT NULL CHECK (reserved_micro >= 0),
        committed_micro INTEGER NOT NULL CHECK (committed_micro >= 0),
        actual_micro INTEGER,
        state TEXT NOT NULL,
        error_code TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS money_guard_attempts_experiment_idx
    ON money_guard_attempts(experiment_id)
    """,
)


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _as_decimal(value: Any, *, code: str) -> Decimal:
    if isinstance(value, bool):
        raise MoneyGuardConfigurationError(code)
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise MoneyGuardConfigurationError(code)
        result = Decimal(str(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            raise MoneyGuardConfigurationError(code) from None
    else:
        raise MoneyGuardConfigurationError(code)
    if not result.is_finite():
        raise MoneyGuardConfigurationError(code)
    return result


def _safe_json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, Mapping):
        return {str(key): _safe_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_safe_json_value(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if isinstance(value, float):
        return str(value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _safe_json_value(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _usage_cost_micro(response: Any, usd_to_cny_upper: Decimal) -> tuple[str, int | None]:
    if not isinstance(response, Mapping):
        return "unknown", None
    usage = response.get("usage")
    if not isinstance(usage, Mapping) or "cost" not in usage:
        return "unknown", None
    raw_cost = usage["cost"]
    if isinstance(raw_cost, bool):
        return "invalid", None
    try:
        cost_usd = _as_decimal(raw_cost, code="money_guard_invalid_usage_cost")
    except MoneyGuardConfigurationError:
        return "invalid", None
    if cost_usd < 0:
        return "invalid", None
    with localcontext() as context:
        context.prec = 80
        micro = (cost_usd * usd_to_cny_upper * Decimal(MICRO_CNY)).to_integral_value(
            rounding=ROUND_CEILING,
        )
    return "known", int(micro)


class MoneyGuardHTTPAttemptClient:
    """为两角色 HTTP attempt 提供持久的整轮 CNY 100 账本。"""

    def __init__(
        self,
        client: Any,
        ledger_path: str | os.PathLike[str] | None = None,
        experiment_id: str = DEFAULT_EXPERIMENT_ID,
        cost_upper_cny: Any = None,
        usd_to_cny_upper: Any = None,
        *,
        path: str | os.PathLike[str] | None = None,
        db_path: str | os.PathLike[str] | None = None,
        database_path: str | os.PathLike[str] | None = None,
        profile: Mapping[str, Any] | None = None,
        model: str | None = None,
        models: Mapping[str, str] | None = None,
        endpoint_summary: Any = None,
        endpoint_digest: str | None = None,
        price_summary: Any = None,
    ):
        supplied_paths = [
            candidate
            for candidate in (ledger_path, path, database_path, db_path)
            if candidate is not None
        ]
        if not supplied_paths:
            raise MoneyGuardConfigurationError("money_guard_ledger_required")
        if any(os.fspath(candidate) != os.fspath(supplied_paths[0]) for candidate in supplied_paths[1:]):
            raise MoneyGuardConfigurationError("money_guard_ledger_ambiguous")
        ledger_path = supplied_paths[0]
        if not isinstance(experiment_id, str) or not experiment_id:
            raise MoneyGuardConfigurationError("money_guard_experiment_invalid")
        if profile is not None and not isinstance(profile, Mapping):
            raise MoneyGuardConfigurationError("money_guard_profile_invalid")

        try:
            raw_roles = tuple(client.registered_roles)
        except (AttributeError, TypeError):
            raise MoneyGuardConfigurationError("money_guard_roles_unregistered") from None
        if len(raw_roles) != len(ALLOWED_ROLES) or set(raw_roles) != set(ALLOWED_ROLES):
            raise MoneyGuardConfigurationError("money_guard_roles_unregistered")
        self._registered_roles = tuple(sorted(raw_roles))

        profile_model = self._model_from_profile(profile)
        if profile_model is not None:
            if model is not None and model != profile_model:
                raise MoneyGuardConfigurationError("money_guard_model_configuration_changed")
            if models is not None and any(
                models.get(role) != profile_model for role in ALLOWED_ROLES
            ):
                raise MoneyGuardConfigurationError("money_guard_model_configuration_changed")
            if model is None and models is None:
                model = profile_model
        self._models = self._validate_models(model, models)
        self._cost_upper_cny = _as_decimal(
            cost_upper_cny,
            code="money_guard_cost_upper_invalid",
        )
        self._usd_to_cny_upper = _as_decimal(
            usd_to_cny_upper,
            code="money_guard_exchange_rate_invalid",
        )
        if self._cost_upper_cny <= 0 or self._usd_to_cny_upper <= 0:
            raise MoneyGuardConfigurationError("money_guard_pricing_invalid")

        with localcontext() as context:
            context.prec = 80
            self._cost_upper_micro = int(
                (self._cost_upper_cny * Decimal(MICRO_CNY)).to_integral_value(
                    rounding=ROUND_CEILING,
                )
            )
        if self._cost_upper_micro > BUDGET_MICRO_CNY:
            raise MoneyGuardConfigurationError("money_guard_cost_upper_exceeds_budget")

        self._experiment_id = experiment_id
        self._client = client
        self._ledger_path = Path(os.fspath(ledger_path)).expanduser().resolve()
        if str(ledger_path) == ":memory:":
            raise MoneyGuardConfigurationError("money_guard_persistent_ledger_required")

        if endpoint_digest is not None:
            endpoint_material = {"endpoint_digest": endpoint_digest}
        elif endpoint_summary is not None:
            endpoint_material = endpoint_summary
        elif profile is not None:
            endpoint_material = profile.get("endpoints", profile)
        else:
            endpoint_material = getattr(client, "endpoint_summary", None)
            if endpoint_material is None:
                endpoint_material = getattr(client, "endpoints", None)
            if endpoint_material is None:
                endpoint_material = getattr(client, "_endpoints", None)
            if endpoint_material is None:
                endpoint_material = {"registered_roles": self._registered_roles}

        self._model_digest = _digest({"models": self._models, "profile": profile})
        self._endpoint_digest = _digest(endpoint_material)
        self._rate_text = _decimal_text(self._usd_to_cny_upper)
        self._price_digest = _digest({
            "cost_upper_cny": _decimal_text(self._cost_upper_cny),
            "usd_to_cny_upper": self._rate_text,
            "price_summary": price_summary,
            "profile": profile,
        })
        self._closed = False
        self._initialize_ledger()

    @staticmethod
    def _model_from_profile(profile: Mapping[str, Any] | None) -> str | None:
        if profile is None:
            return None
        provider = profile.get("provider")
        profile_model = profile.get("model")
        if isinstance(profile_model, Mapping):
            profile_model = profile_model.get("name", profile_model.get("id"))
        if provider is None and profile_model is None:
            return None
        if not isinstance(profile_model, str):
            raise MoneyGuardConfigurationError("money_guard_profile_invalid")
        if "/" in profile_model:
            if provider is not None and provider != profile_model.split("/", 1)[0]:
                raise MoneyGuardConfigurationError("money_guard_profile_invalid")
            return profile_model
        if not isinstance(provider, str) or not provider:
            raise MoneyGuardConfigurationError("money_guard_profile_invalid")
        return f"{provider}/{profile_model}"

    @staticmethod
    def _validate_models(
        model: str | None,
        models: Mapping[str, str] | None,
    ) -> dict[str, str]:
        if model is not None and models is not None:
            raise MoneyGuardConfigurationError("money_guard_model_configuration_ambiguous")
        if models is None:
            selected = {role: model or ALLOWED_MODEL for role in ALLOWED_ROLES}
        else:
            selected = dict(models)
        if set(selected) != set(ALLOWED_ROLES):
            raise MoneyGuardConfigurationError("money_guard_model_binding_invalid")
        if any(selected[role] != ALLOWED_MODEL for role in ALLOWED_ROLES):
            raise MoneyGuardConfigurationError("money_guard_model_forbidden")
        return {role: selected[role] for role in ALLOWED_ROLES}

    @property
    def registered_roles(self) -> tuple[str, ...]:
        return self._registered_roles

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._ledger_path), timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_ledger(self) -> None:
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA:
                    connection.execute(statement)
                row = connection.execute(
                    """
                    SELECT budget_micro, cost_upper_micro, usd_to_cny_upper,
                           model_digest, endpoint_digest, price_digest
                    FROM money_guard_experiments
                    WHERE experiment_id = ?
                    """,
                    (self._experiment_id,),
                ).fetchone()
                expected = (
                    BUDGET_MICRO_CNY,
                    self._cost_upper_micro,
                    self._rate_text,
                    self._model_digest,
                    self._endpoint_digest,
                    self._price_digest,
                )
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO money_guard_experiments (
                            experiment_id, budget_micro, cost_upper_micro,
                            usd_to_cny_upper, model_digest, endpoint_digest,
                            price_digest
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (self._experiment_id, *expected),
                    )
                elif tuple(row) != expected:
                    raise MoneyGuardConfigurationError(
                        "money_guard_configuration_changed",
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise MoneyGuardError("money_guard_closed")

    def _validate_attempt(self, role: str, payload: Any) -> None:
        if role not in self._registered_roles:
            raise MoneyGuardError("endpoint_role_unregistered")
        if not isinstance(payload, Mapping):
            raise MoneyGuardError("money_guard_invalid_payload")
        payload_model = payload.get("model", self._models[role])
        if payload_model != self._models[role]:
            raise MoneyGuardError("money_guard_model_forbidden")

    def _reserve(self, role: str) -> int:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT budget_micro, blocked_reason
                FROM money_guard_experiments
                WHERE experiment_id = ?
                """,
                (self._experiment_id,),
            ).fetchone()
            if row is None:
                raise MoneyGuardError("money_guard_ledger_missing")
            budget_micro, blocked_reason = row
            if blocked_reason is not None:
                raise MoneyGuardError("money_guard_blocked")
            committed = connection.execute(
                """
                SELECT COALESCE(SUM(committed_micro), 0)
                FROM money_guard_attempts
                WHERE experiment_id = ?
                """,
                (self._experiment_id,),
            ).fetchone()[0]
            if int(committed) + self._cost_upper_micro > int(budget_micro):
                raise MoneyGuardError("money_budget_exhausted")
            cursor = connection.execute(
                """
                INSERT INTO money_guard_attempts (
                    experiment_id, role, reserved_micro, committed_micro, state
                ) VALUES (?, ?, ?, ?, 'reserved')
                """,
                (
                    self._experiment_id,
                    role,
                    self._cost_upper_micro,
                    self._cost_upper_micro,
                ),
            )
            if cursor.lastrowid is None:
                raise MoneyGuardError("money_guard_ledger_failure")
            return int(cursor.lastrowid)

    def _keep_reserved(self, attempt_id: int, error_code: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE money_guard_attempts
                SET state = 'unknown', error_code = ?
                WHERE attempt_id = ? AND state = 'reserved'
                """,
                (error_code, attempt_id),
            )

    def _block_attempt(self, attempt_id: int, error_code: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE money_guard_attempts
                SET state = 'blocked', error_code = ?
                WHERE attempt_id = ? AND state = 'reserved'
                """,
                (error_code, attempt_id),
            )
            connection.execute(
                """
                UPDATE money_guard_experiments
                SET blocked_reason = COALESCE(blocked_reason, ?)
                WHERE experiment_id = ?
                """,
                (error_code, self._experiment_id),
            )

    def _settle(self, attempt_id: int, actual_micro: int) -> str:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT reserved_micro, state
                FROM money_guard_attempts
                WHERE attempt_id = ? AND experiment_id = ?
                """,
                (attempt_id, self._experiment_id),
            ).fetchone()
            if row is None or row[1] != "reserved":
                raise MoneyGuardError("money_guard_attempt_not_pending")
            reserved_micro = int(row[0])
            blocked = connection.execute(
                """
                SELECT blocked_reason
                FROM money_guard_experiments
                WHERE experiment_id = ?
                """,
                (self._experiment_id,),
            ).fetchone()[0]
            if blocked is not None:
                connection.execute(
                    """
                    UPDATE money_guard_attempts
                    SET state = 'blocked', error_code = 'money_guard_blocked'
                    WHERE attempt_id = ?
                    """,
                    (attempt_id,),
                )
                return "blocked"
            if actual_micro > reserved_micro:
                connection.execute(
                    """
                    UPDATE money_guard_attempts
                    SET state = 'blocked', actual_micro = ?,
                        error_code = 'money_guard_billing_overrun'
                    WHERE attempt_id = ?
                    """,
                    (actual_micro, attempt_id),
                )
                connection.execute(
                    """
                    UPDATE money_guard_experiments
                    SET blocked_reason = 'money_guard_billing_overrun'
                    WHERE experiment_id = ?
                    """,
                    (self._experiment_id,),
                )
                return "overrun"
            connection.execute(
                """
                UPDATE money_guard_attempts
                SET state = 'settled', actual_micro = ?, committed_micro = ?,
                    error_code = NULL
                WHERE attempt_id = ?
                """,
                (actual_micro, actual_micro, attempt_id),
            )
            return "settled"

    async def attempt(self, role: str, payload: dict, timeout: float) -> dict:
        self._ensure_open()
        self._validate_attempt(role, payload)
        attempt_id = self._reserve(role)
        try:
            result = self._client.attempt(role, payload, timeout)
            response = await result if inspect.isawaitable(result) else result
        except BaseException:
            self._keep_reserved(attempt_id, "underlying_attempt_failed")
            raise

        status, actual_micro = _usage_cost_micro(response, self._usd_to_cny_upper)
        if status == "unknown":
            self._keep_reserved(attempt_id, "money_guard_usage_unknown")
            raise MoneyGuardError("money_guard_usage_unknown")
        if status == "invalid":
            self._block_attempt(attempt_id, "money_guard_invalid_usage_cost")
            raise MoneyGuardError("money_guard_invalid_usage_cost")

        settlement = self._settle(attempt_id, actual_micro or 0)
        if settlement == "overrun":
            raise MoneyGuardError("money_guard_billing_overrun")
        if settlement == "blocked":
            raise MoneyGuardError("money_guard_blocked")
        return response

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        result = self._client.close()
        if inspect.isawaitable(result):
            await result


PersistentMoneyBudgetHTTPAttemptClient = MoneyGuardHTTPAttemptClient
MoneyBudgetHTTPAttemptClient = MoneyGuardHTTPAttemptClient
MoneyGuard = MoneyGuardHTTPAttemptClient


__all__ = [
    "ALLOWED_MODEL",
    "ALLOWED_ROLES",
    "BUDGET_CNY",
    "DEFAULT_EXPERIMENT_ID",
    "MoneyBudgetHTTPAttemptClient",
    "MoneyGuard",
    "MoneyGuardConfigurationError",
    "MoneyGuardError",
    "MoneyGuardHTTPAttemptClient",
    "PersistentMoneyBudgetHTTPAttemptClient",
]
