from __future__ import annotations

import inspect
import json
import math
import os
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
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
_BILLING_MODES = frozenset({"local_token_free", "remote_actual"})


class MoneyGuardError(HarnessError):
    """货币账本拒绝或阻断一次 attempt。"""


class MoneyGuardNotSent(MoneyGuardError):
    """货币门在调用底层物理客户端之前明确拒绝。"""


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

_VERSIONED_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS money_guard_contract_versions (
        experiment_id TEXT NOT NULL
            REFERENCES money_guard_experiments(experiment_id),
        contract_version INTEGER NOT NULL,
        contract_digest TEXT NOT NULL,
        previous_contract_digest TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (experiment_id, contract_version),
        UNIQUE (experiment_id, contract_digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS money_guard_contracts (
        experiment_id TEXT NOT NULL
            REFERENCES money_guard_experiments(experiment_id),
        contract_version INTEGER NOT NULL,
        contract_digest TEXT NOT NULL,
        role_digest TEXT NOT NULL,
        role TEXT NOT NULL,
        model TEXT NOT NULL,
        endpoint_digest TEXT NOT NULL,
        quote_cny TEXT NOT NULL,
        quote_micro INTEGER NOT NULL CHECK (quote_micro >= 0),
        billing_mode TEXT NOT NULL,
        usage_source TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (experiment_id, contract_version, role),
        FOREIGN KEY (experiment_id, contract_version)
            REFERENCES money_guard_contract_versions(experiment_id, contract_version)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS money_guard_contracts_role_idx
    ON money_guard_contracts(experiment_id, role, contract_version)
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


def _micro_from_cny(value: Decimal) -> int:
    with localcontext() as context:
        context.prec = 80
        return int(
            (value * Decimal(MICRO_CNY)).to_integral_value(
                rounding=ROUND_CEILING,
            )
        )


@dataclass(frozen=True)
class RoleBillingContract:
    """一次业务角色的不可变收费与模型绑定合同。"""

    role: str
    model: str
    endpoint_digest: str
    quote_cny: Decimal
    billing_mode: str
    usage_source: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, str) or not self.role.strip():
            raise MoneyGuardConfigurationError("money_guard_contract_invalid")
        if not isinstance(self.model, str) or not self.model.strip():
            raise MoneyGuardConfigurationError("money_guard_contract_invalid")
        if not isinstance(self.endpoint_digest, str) or not self.endpoint_digest.strip():
            raise MoneyGuardConfigurationError("money_guard_contract_invalid")
        if self.billing_mode not in _BILLING_MODES:
            raise MoneyGuardConfigurationError("money_guard_billing_mode_invalid")
        if not isinstance(self.usage_source, str) or not self.usage_source.strip():
            raise MoneyGuardConfigurationError("money_guard_usage_source_invalid")
        quote = _as_decimal(self.quote_cny, code="money_guard_contract_quote_invalid")
        if quote < 0:
            raise MoneyGuardConfigurationError("money_guard_contract_quote_invalid")
        object.__setattr__(self, "quote_cny", quote)

    @property
    def quote_micro(self) -> int:
        return _micro_from_cny(self.quote_cny)

    @property
    def digest(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model": self.model,
            "endpoint_digest": self.endpoint_digest,
            "quote_cny": _decimal_text(self.quote_cny),
            "billing_mode": self.billing_mode,
            "usage_source": self.usage_source,
        }

def _coerce_contract(value: RoleBillingContract | Mapping[str, Any]) -> RoleBillingContract:
    if isinstance(value, RoleBillingContract):
        return value
    if isinstance(value, Mapping):
        try:
            return RoleBillingContract(**dict(value))
        except TypeError as exc:
            raise MoneyGuardConfigurationError("money_guard_contract_invalid") from exc
    raise MoneyGuardConfigurationError("money_guard_contract_invalid")


def _normalize_contracts(
    value: Mapping[str, RoleBillingContract | Mapping[str, Any]]
    | Iterable[RoleBillingContract | Mapping[str, Any]]
    | None,
) -> dict[str, RoleBillingContract] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        items = []
        for role, raw_contract in value.items():
            contract = _coerce_contract(raw_contract)
            if contract.role != role:
                raise MoneyGuardConfigurationError("money_guard_contract_invalid")
            items.append(contract)
    else:
        try:
            items = [_coerce_contract(item) for item in value]
        except TypeError as exc:
            raise MoneyGuardConfigurationError("money_guard_contract_invalid") from exc
    result: dict[str, RoleBillingContract] = {}
    for contract in items:
        if contract.role in result:
            raise MoneyGuardConfigurationError("money_guard_contract_duplicate")
        result[contract.role] = contract
    return result


def _contracts_digest(version: int, contracts: Mapping[str, RoleBillingContract]) -> str:
    return _digest({
        "contract_version": version,
        "contracts": [contracts[role].to_dict() for role in sorted(contracts)],
    })


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


def _connection_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _ensure_attempt_columns(connection: sqlite3.Connection) -> None:
    columns = _connection_columns(connection, "money_guard_attempts")
    additions = {
        "contract_version": "INTEGER NOT NULL DEFAULT 1",
        "contract_digest": "TEXT NOT NULL DEFAULT ''",
        "contract_quote_micro": "INTEGER NOT NULL DEFAULT 0",
        "usage_total_tokens": "INTEGER",
        "usage_cost_usd": "TEXT",
    }
    for name, definition in additions.items():
        if name not in columns:
            connection.execute(
                f"ALTER TABLE money_guard_attempts ADD COLUMN {name} {definition}"
            )


def _active_contract_state(
    connection: sqlite3.Connection,
    experiment_id: str,
) -> tuple[int, str, dict[str, RoleBillingContract]]:
    version_row = connection.execute(
        """
        SELECT contract_version, contract_digest
        FROM money_guard_contract_versions
        WHERE experiment_id = ?
        ORDER BY contract_version DESC
        LIMIT 1
        """,
        (experiment_id,),
    ).fetchone()
    if version_row is None:
        raise MoneyGuardConfigurationError("money_guard_contracts_missing")
    version, digest = int(version_row[0]), str(version_row[1])
    rows = connection.execute(
        """
        SELECT c.contract_version, c.role, c.model, c.endpoint_digest,
               c.quote_cny, c.billing_mode, c.usage_source,
               c.contract_digest, c.role_digest, v.contract_digest
        FROM money_guard_contracts AS c
        JOIN money_guard_contract_versions AS v
          ON v.experiment_id = c.experiment_id
         AND v.contract_version = c.contract_version
        WHERE c.experiment_id = ?
          AND c.contract_version = (
              SELECT MAX(c2.contract_version)
              FROM money_guard_contracts AS c2
              WHERE c2.experiment_id = c.experiment_id
                AND c2.role = c.role
          )
        ORDER BY c.role
        """,
        (experiment_id,),
    ).fetchall()
    contracts: dict[str, RoleBillingContract] = {}
    for row in rows:
        contract = RoleBillingContract(
            role=str(row[1]),
            model=str(row[2]),
            endpoint_digest=str(row[3]),
            quote_cny=_as_decimal(
                str(row[4]), code="money_guard_contract_quote_invalid",
            ),
            billing_mode=str(row[5]),
            usage_source=str(row[6]),
        )
        if str(row[7]) != str(row[9]) or str(row[8]) != contract.digest:
            raise MoneyGuardConfigurationError("money_guard_contracts_corrupt")
        contracts[contract.role] = contract
    if not contracts or _contracts_digest(version, contracts) != digest:
        raise MoneyGuardConfigurationError("money_guard_contracts_corrupt")
    return version, digest, contracts


def read_billing_contract(
    path: str | os.PathLike[str],
    experiment_id: str,
) -> dict[str, Any]:
    """以 SQLite 只读模式读取既有合同，不创建或迁移任何表。"""
    if str(path) == ":memory:":
        raise MoneyGuardConfigurationError("money_guard_persistent_ledger_required")
    if not isinstance(experiment_id, str) or not experiment_id:
        raise MoneyGuardConfigurationError("money_guard_experiment_invalid")
    ledger_path = Path(os.fspath(path)).expanduser().resolve()
    if not ledger_path.is_file():
        raise MoneyGuardConfigurationError("money_guard_ledger_missing")
    database_uri = ledger_path.as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(database_uri, uri=True)
    except sqlite3.Error as exc:
        raise MoneyGuardConfigurationError("money_guard_ledger_unreadable") from exc
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not {
            "money_guard_experiments",
            "money_guard_contract_versions",
            "money_guard_contracts",
        } <= tables:
            raise MoneyGuardConfigurationError("money_guard_contracts_missing")
        experiment = connection.execute(
            """
            SELECT usd_to_cny_upper
            FROM money_guard_experiments
            WHERE experiment_id = ?
            """,
            (experiment_id,),
        ).fetchone()
        if experiment is None:
            raise MoneyGuardConfigurationError("money_guard_ledger_missing")
        version, digest, contracts = _active_contract_state(connection, experiment_id)
        rate = _as_decimal(
            experiment[0], code="money_guard_exchange_rate_invalid",
        )
        if rate <= 0:
            raise MoneyGuardConfigurationError("money_guard_pricing_invalid")
        return {
            "billing_contract_digest": digest,
            "contracts": dict(contracts),
            "usd_to_cny_upper": rate,
        }
    except sqlite3.Error as exc:
        raise MoneyGuardConfigurationError("money_guard_contracts_unreadable") from exc
    finally:
        connection.close()


def _bootstrap_versioned_contracts(
    connection: sqlite3.Connection,
    experiment_id: str,
    *,
    model: str,
    endpoint_digest: str,
    quote_micro: int,
) -> None:
    existing = connection.execute(
        "SELECT 1 FROM money_guard_contract_versions WHERE experiment_id = ? LIMIT 1",
        (experiment_id,),
    ).fetchone()
    if existing is None:
        quote_cny = Decimal(quote_micro) / Decimal(MICRO_CNY)
        contracts = {
            role: RoleBillingContract(
                role=role,
                model=model,
                endpoint_digest=endpoint_digest,
                quote_cny=quote_cny,
                billing_mode="remote_actual",
                usage_source="cost_usd",
            )
            for role in ALLOWED_ROLES
        }
        digest = _contracts_digest(1, contracts)
        connection.execute(
            """
            INSERT INTO money_guard_contract_versions(
                experiment_id, contract_version, contract_digest,
                previous_contract_digest
            ) VALUES (?, 1, ?, NULL)
            """,
            (experiment_id, digest),
        )
        for contract in contracts.values():
            connection.execute(
                """
                INSERT INTO money_guard_contracts(
                    experiment_id, contract_version, contract_digest, role_digest,
                    role, model, endpoint_digest, quote_cny, quote_micro,
                    billing_mode, usage_source
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    digest,
                    contract.digest,
                    contract.role,
                    contract.model,
                    contract.endpoint_digest,
                    _decimal_text(contract.quote_cny),
                    contract.quote_micro,
                    contract.billing_mode,
                    contract.usage_source,
                ),
            )
    _active_contract_state(connection, experiment_id)
    legacy = connection.execute(
        "SELECT contract_digest FROM money_guard_contract_versions "
        "WHERE experiment_id=? AND contract_version=1", (experiment_id,),
    ).fetchone()
    if legacy is None:
        raise MoneyGuardConfigurationError("money_guard_contracts_corrupt")
    connection.execute(
        """
        UPDATE money_guard_attempts
        SET contract_version = 1, contract_digest = ?, contract_quote_micro = ?
        WHERE experiment_id = ? AND (contract_digest IS NULL OR contract_digest = '')
        """,
        (legacy[0], quote_micro, experiment_id),
    )


def _prepare_existing_versioned_ledger(
    connection: sqlite3.Connection,
    experiment_id: str,
    *,
    default_model: str = ALLOWED_MODEL,
) -> tuple[int, str, dict[str, RoleBillingContract], Decimal]:
    experiment = connection.execute(
        """
        SELECT budget_micro, usd_to_cny_upper, endpoint_digest, cost_upper_micro
        FROM money_guard_experiments
        WHERE experiment_id = ?
        """,
        (experiment_id,),
    ).fetchone()
    if experiment is None:
        raise MoneyGuardConfigurationError("money_guard_ledger_missing")
    if int(experiment[0]) != BUDGET_MICRO_CNY:
        raise MoneyGuardConfigurationError("money_guard_budget_invalid")
    for statement in _VERSIONED_SCHEMA:
        connection.execute(statement)
    _ensure_attempt_columns(connection)
    _bootstrap_versioned_contracts(
        connection,
        experiment_id,
        model=default_model,
        endpoint_digest=str(experiment[2]),
        quote_micro=int(experiment[3]),
    )
    version, digest, contracts = _active_contract_state(connection, experiment_id)
    try:
        rate = _as_decimal(experiment[1], code="money_guard_exchange_rate_invalid")
    except MoneyGuardConfigurationError:
        raise
    if rate <= 0:
        raise MoneyGuardConfigurationError("money_guard_pricing_invalid")
    return version, digest, contracts, rate


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
        self._client_roles = frozenset(raw_roles)
        self._registered_roles = tuple(sorted(raw_roles))
        self._active_attempt_ids: set[int] = set()
        self._acknowledged_unknown_attempts: set[int] = set()

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

        self._cost_upper_micro = _micro_from_cny(self._cost_upper_cny)
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

    @property
    def billing_contract_digest(self) -> str:
        return self._billing_contract_digest

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
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            _prepare_existing_versioned_ledger(connection, self._experiment_id)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._refresh_contract_state()

    def _refresh_contract_state(self) -> None:
        connection = self._connect()
        try:
            version, digest, contracts = _active_contract_state(
                connection, self._experiment_id,
            )
        finally:
            connection.close()
        self._contract_version = version
        self._billing_contract_digest = digest
        self._contracts = contracts

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
            _, digest, contracts = _active_contract_state(connection, self._experiment_id)
            contract = contracts.get(role)
            if (digest != self._billing_contract_digest or contract is None
                    or contract.model != self._models[role]
                    or contract.endpoint_digest != self._endpoint_digest
                    or contract.quote_micro != self._cost_upper_micro
                    or contract.billing_mode != "remote_actual"
                    or contract.usage_source != "cost_usd"):
                raise MoneyGuardConfigurationError("money_guard_contract_configuration_changed")
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
                    experiment_id, role, reserved_micro, committed_micro, state,
                    contract_version, contract_digest, contract_quote_micro
                ) VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)
                """,
                (self._experiment_id, role, self._cost_upper_micro,
                 self._cost_upper_micro, self._contract_version,
                 self._billing_contract_digest, self._cost_upper_micro),
            )
            if cursor.lastrowid is None:
                raise MoneyGuardError("money_guard_ledger_failure")
            return int(cursor.lastrowid)

    def _record_usage(
        self,
        attempt_id: int,
        *,
        total_tokens: int | None,
        cost_usd: str | None,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE money_guard_attempts
                SET usage_total_tokens = ?, usage_cost_usd = ?
                WHERE attempt_id = ? AND experiment_id = ?
                """,
                (total_tokens, cost_usd, attempt_id, self._experiment_id),
            )

    @staticmethod
    def _usage_for_contract(
        response: Any,
        contract: RoleBillingContract,
        usd_to_cny_upper: Decimal,
    ) -> tuple[str, int | None, int | None, str | None]:
        if not isinstance(response, Mapping):
            return "unknown", None, None, None
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            return "unknown", None, None, None
        total = usage.get("total_tokens")
        total_tokens = total if type(total) is int and total >= 0 else None
        if contract.billing_mode == "local_token_free":
            if total_tokens is None:
                return "unknown", None, total_tokens, None
            return "known", 0, total_tokens, None
        status, actual_micro = _usage_cost_micro(response, usd_to_cny_upper)
        raw_cost = usage.get("cost")
        cost_text = None
        if status == "known":
            cost_text = _decimal_text(_as_decimal(
                raw_cost, code="money_guard_invalid_usage_cost",
            ))
        return status, actual_micro, total_tokens, cost_text

    def acknowledge_unknown_attempts(self, attempt_ids: Iterable[int]) -> None:
        values = set()
        try:
            for attempt_id in attempt_ids:
                if type(attempt_id) is not int or attempt_id < 1:
                    raise ValueError
                values.add(attempt_id)
        except (TypeError, ValueError):
            raise MoneyGuardConfigurationError("money_guard_unknown_ack_invalid") from None
        self._acknowledged_unknown_attempts.update(values)

    def register_contract(
        self,
        contract: RoleBillingContract | Mapping[str, Any],
        expected_previous_contract_digest: str | None = None,
        *,
        contract_version: int | None = None,
        expected_previous_role_digest: str | None = None,
    ) -> str:
        """追加合同版本；修订已有角色还须明确匹配旧角色摘要，不改旧费用与请求。"""
        self._ensure_open()
        contract = _coerce_contract(contract)
        if contract.quote_micro > BUDGET_MICRO_CNY:
            raise MoneyGuardConfigurationError("money_guard_contract_quote_exceeds_budget")
        if contract.role not in self._client_roles:
            raise MoneyGuardConfigurationError("endpoint_role_unregistered")
        if not isinstance(expected_previous_contract_digest, str):
            raise MoneyGuardConfigurationError("money_guard_contract_digest_required")
        with self._transaction() as connection:
            current_version, current_digest, current = _active_contract_state(
                connection, self._experiment_id,
            )
            if current_digest != expected_previous_contract_digest:
                raise MoneyGuardConfigurationError(
                    "money_guard_contract_digest_mismatch",
                )
            if contract.role in current:
                if expected_previous_role_digest is None:
                    raise MoneyGuardConfigurationError("money_guard_contract_already_registered")
                if expected_previous_role_digest != current[contract.role].digest:
                    raise MoneyGuardConfigurationError("money_guard_role_digest_mismatch")
            elif expected_previous_role_digest is not None:
                raise MoneyGuardConfigurationError("money_guard_contract_unregistered")
            blocked = connection.execute(
                """
                SELECT blocked_reason FROM money_guard_experiments
                WHERE experiment_id = ?
                """,
                (self._experiment_id,),
            ).fetchone()
            if blocked is None:
                raise MoneyGuardConfigurationError("money_guard_ledger_missing")
            if blocked[0] is not None:
                raise MoneyGuardConfigurationError("money_guard_blocked")
            next_version = current_version + 1
            if contract_version is not None and contract_version != next_version:
                raise MoneyGuardConfigurationError("money_guard_contract_version_invalid")
            next_contracts = dict(current)
            next_contracts[contract.role] = contract
            next_digest = _contracts_digest(next_version, next_contracts)
            connection.execute(
                """
                INSERT INTO money_guard_contract_versions(
                    experiment_id, contract_version, contract_digest,
                    previous_contract_digest
                ) VALUES (?, ?, ?, ?)
                """,
                (self._experiment_id, next_version, next_digest, current_digest),
            )
            connection.execute(
                """
                INSERT INTO money_guard_contracts(
                    experiment_id, contract_version, contract_digest, role_digest,
                    role, model, endpoint_digest, quote_cny, quote_micro,
                    billing_mode, usage_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._experiment_id,
                    next_version,
                    next_digest,
                    contract.digest,
                    contract.role,
                    contract.model,
                    contract.endpoint_digest,
                    _decimal_text(contract.quote_cny),
                    contract.quote_micro,
                    contract.billing_mode,
                    contract.usage_source,
                ),
            )
        self._refresh_contract_state()
        return self._billing_contract_digest

    def _refresh_after_external_contract_change(self) -> None:
        self._refresh_contract_state()
        self._registered_roles = tuple(sorted(self._contracts))
        self._models = {role: contract.model for role, contract in self._contracts.items()}

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
        try:
            self._ensure_open()
            self._validate_attempt(role, payload)
            attempt_id = self._reserve(role)
        except MoneyGuardError as exc:
            raise MoneyGuardNotSent(exc.code, exc.status_code, exc.details) from exc
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

    def _attempt_is_reserved(self, attempt_id: int) -> bool:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT state FROM money_guard_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        finally:
            connection.close()
        return row is not None and row[0] == "reserved"

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        result = self._client.close()
        if inspect.isawaitable(result):
            await result


class VersionedMoneyGuardHTTPAttemptClient(MoneyGuardHTTPAttemptClient):
    """绑定既有 v1 实验并按角色合同结算的完整链路账本客户端。"""

    def __init__(
        self,
        client: Any,
        ledger_path: str | os.PathLike[str] | None = None,
        experiment_id: str = DEFAULT_EXPERIMENT_ID,
        *,
        path: str | os.PathLike[str] | None = None,
        db_path: str | os.PathLike[str] | None = None,
        database_path: str | os.PathLike[str] | None = None,
        contracts: Mapping[str, RoleBillingContract | Mapping[str, Any]]
        | Iterable[RoleBillingContract | Mapping[str, Any]]
        | None = None,
        role_contracts: Mapping[str, RoleBillingContract | Mapping[str, Any]]
        | Iterable[RoleBillingContract | Mapping[str, Any]]
        | None = None,
        billing_contracts: Mapping[str, RoleBillingContract | Mapping[str, Any]]
        | Iterable[RoleBillingContract | Mapping[str, Any]]
        | None = None,
        usd_to_cny_upper: Any = None,
        acknowledged_unknown_attempts: Iterable[int] = (),
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
        if str(ledger_path) == ":memory:":
            raise MoneyGuardConfigurationError("money_guard_persistent_ledger_required")
        if not isinstance(experiment_id, str) or not experiment_id:
            raise MoneyGuardConfigurationError("money_guard_experiment_invalid")
        if not os.path.exists(os.fspath(ledger_path)):
            raise MoneyGuardConfigurationError("money_guard_ledger_missing")
        try:
            raw_roles = tuple(client.registered_roles)
        except (AttributeError, TypeError):
            raise MoneyGuardConfigurationError("money_guard_roles_unregistered") from None
        if len(set(raw_roles)) != len(raw_roles):
            raise MoneyGuardConfigurationError("money_guard_roles_unregistered")
        self._client_roles = frozenset(raw_roles)
        self._client = client
        self._experiment_id = experiment_id
        self._ledger_path = Path(os.fspath(ledger_path)).expanduser().resolve()
        self._closed = False
        self._active_attempt_ids: set[int] = set()
        self._acknowledged_unknown_attempts: set[int] = set()
        self.acknowledge_unknown_attempts(acknowledged_unknown_attempts)

        supplied_contract_sets = [item for item in (contracts, role_contracts, billing_contracts)
                                  if item is not None]
        if len(supplied_contract_sets) > 1:
            raise MoneyGuardConfigurationError("money_guard_contracts_ambiguous")
        self._supplied_contracts = _normalize_contracts(
            supplied_contract_sets[0] if supplied_contract_sets else None,
        )

        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                version, digest, active_contracts, rate = _prepare_existing_versioned_ledger(
                    connection, experiment_id,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.close()
        if usd_to_cny_upper is not None:
            supplied_rate = _as_decimal(
                usd_to_cny_upper, code="money_guard_exchange_rate_invalid",
            )
            if supplied_rate != rate:
                raise MoneyGuardConfigurationError("money_guard_configuration_changed")
        if any(role not in self._client_roles for role in active_contracts):
            raise MoneyGuardConfigurationError("money_guard_roles_unregistered")
        if self._supplied_contracts is not None:
            for role, supplied in self._supplied_contracts.items():
                current = active_contracts.get(role)
                if current is None:
                    raise MoneyGuardConfigurationError("money_guard_contract_unregistered")
                if current != supplied:
                    raise MoneyGuardConfigurationError("money_guard_contract_configuration_changed")

        self._usd_to_cny_upper = rate
        self._contract_version = version
        self._billing_contract_digest = digest
        self._contracts = active_contracts
        self._registered_roles = tuple(sorted(active_contracts))
        self._models = {role: contract.model for role, contract in active_contracts.items()}

    def _initialize_ledger(self) -> None:
        raise AssertionError("版本化货币账本必须绑定既有实验")

    def _reserve(self, role: str) -> int:
        contract = self._contracts[role]
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
            pending = {
                int(item[0])
                for item in connection.execute(
                    """
                    SELECT attempt_id
                    FROM money_guard_attempts
                    WHERE experiment_id = ? AND state IN ('reserved', 'unknown')
                    """,
                    (self._experiment_id,),
                ).fetchall()
                if int(item[0]) not in self._active_attempt_ids
                and int(item[0]) not in self._acknowledged_unknown_attempts
            }
            if pending:
                raise MoneyGuardError("unknown_cost_ack_required")
            committed = connection.execute(
                """
                SELECT COALESCE(SUM(committed_micro), 0)
                FROM money_guard_attempts
                WHERE experiment_id = ?
                """,
                (self._experiment_id,),
            ).fetchone()[0]
            if int(committed) + contract.quote_micro > int(budget_micro):
                raise MoneyGuardError("money_budget_exhausted")
            cursor = connection.execute(
                """
                INSERT INTO money_guard_attempts (
                    experiment_id, role, reserved_micro, committed_micro, state,
                    contract_version, contract_digest, contract_quote_micro
                ) VALUES (?, ?, ?, ?, 'reserved', ?, ?, ?)
                """,
                (
                    self._experiment_id,
                    role,
                    contract.quote_micro,
                    contract.quote_micro,
                    self._contract_version,
                    self._billing_contract_digest,
                    contract.quote_micro,
                ),
            )
            if cursor.lastrowid is None:
                raise MoneyGuardError("money_guard_ledger_failure")
            attempt_id = int(cursor.lastrowid)
            self._active_attempt_ids.add(attempt_id)
            return attempt_id

    def _attempt_is_reserved(self, attempt_id: int) -> bool:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT state FROM money_guard_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        finally:
            connection.close()
        return row is not None and row[0] == "reserved"

    async def attempt(self, role: str, payload: dict, timeout: float) -> dict:
        try:
            self._ensure_open()
            self._validate_attempt(role, payload)
            contract = self._contracts[role]
            attempt_id = self._reserve(role)
        except MoneyGuardError as exc:
            raise MoneyGuardNotSent(exc.code, exc.status_code, exc.details) from exc
        try:
            result = self._client.attempt(role, payload, timeout)
            response = await result if inspect.isawaitable(result) else result
            status, actual_micro, total_tokens, cost_usd = self._usage_for_contract(
                response, contract, self._usd_to_cny_upper,
            )
            self._record_usage(
                attempt_id,
                total_tokens=total_tokens,
                cost_usd=cost_usd,
            )
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
        except BaseException:
            if self._attempt_is_reserved(attempt_id):
                self._keep_reserved(attempt_id, "underlying_attempt_failed")
            raise
        finally:
            self._active_attempt_ids.discard(attempt_id)

    def register_contract(
        self,
        contract: RoleBillingContract | Mapping[str, Any],
        expected_previous_contract_digest: str | None = None,
        *,
        contract_version: int | None = None,
        expected_previous_role_digest: str | None = None,
    ) -> str:
        result = super().register_contract(
            contract,
            expected_previous_contract_digest,
            contract_version=contract_version,
            expected_previous_role_digest=expected_previous_role_digest,
        )
        self._refresh_after_external_contract_change()
        return result


PersistentMoneyBudgetHTTPAttemptClient = MoneyGuardHTTPAttemptClient
MoneyBudgetHTTPAttemptClient = MoneyGuardHTTPAttemptClient
MoneyGuard = MoneyGuardHTTPAttemptClient
PersistentVersionedMoneyBudgetHTTPAttemptClient = VersionedMoneyGuardHTTPAttemptClient
VersionedMoneyGuard = VersionedMoneyGuardHTTPAttemptClient


__all__ = [
    "ALLOWED_MODEL",
    "ALLOWED_ROLES",
    "BUDGET_CNY",
    "BUDGET_MICRO_CNY",
    "DEFAULT_EXPERIMENT_ID",
    "MICRO_CNY",
    "MoneyBudgetHTTPAttemptClient",
    "MoneyGuard",
    "MoneyGuardConfigurationError",
    "MoneyGuardError",
    "MoneyGuardHTTPAttemptClient",
    "PersistentMoneyBudgetHTTPAttemptClient",
    "PersistentVersionedMoneyBudgetHTTPAttemptClient",
    "RoleBillingContract",
    "VersionedMoneyGuard",
    "VersionedMoneyGuardHTTPAttemptClient",
]
