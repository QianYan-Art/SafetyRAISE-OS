from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal
from dataclasses import replace

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.money_guard import (
    ALLOWED_MODEL,
    MoneyGuardConfigurationError,
    MoneyGuardError,
    MoneyGuardHTTPAttemptClient,
    MoneyGuardNotSent,
    RoleBillingContract,
    VersionedMoneyGuardHTTPAttemptClient,
    read_billing_contract,
)


class LegacyClient:
    registered_roles = ("generator", "reviewer")

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.close_calls = 0

    async def attempt(self, role, payload, timeout):
        self.calls.append((role, payload, timeout))
        return self.response

    async def close(self):
        self.close_calls += 1


class BusinessClient:
    registered_roles = ("embedding", "expert", "generator", "reviewer")

    def __init__(self, responses):
        self.responses = dict(responses)
        self.calls = []
        self.close_calls = 0

    async def attempt(self, role, payload, timeout):
        self.calls.append((role, payload, timeout))
        response = self.responses[role]
        return response() if callable(response) else response

    async def close(self):
        self.close_calls += 1


class BlockingBusinessClient(BusinessClient):
    def __init__(self):
        super().__init__({"generator": {"usage": {"cost": "0"}}})
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def attempt(self, role, payload, timeout):
        self.calls.append((role, payload, timeout))
        self.started.set()
        await self.release.wait()
        return {"usage": {"cost": "0"}}


def seed_legacy(path, *, cost="24", response=None):
    client = LegacyClient(response or {"usage": {"cost": "0"}})
    guard = MoneyGuardHTTPAttemptClient(
        client,
        path,
        "report-evidence-v1-Q-CNY100",
        Decimal(cost),
        Decimal("7"),
    )
    return guard, client


def contract(role, model, quote, mode, source):
    return RoleBillingContract(
        role=role,
        model=model,
        endpoint_digest="endpoint-" + role,
        quote_cny=quote,
        billing_mode=mode,
        usage_source=source,
    )


def query(path, sql, args=()):
    with sqlite3.connect(path) as connection:
        return connection.execute(sql, args).fetchall()


def test_explicit_role_revision_preserves_old_contract_and_unknown_reserve(tmp_path):
    path = tmp_path / "money.sqlite3"
    old, _ = seed_legacy(path, response={"output": "合成未知费用"})

    async def seed():
        with pytest.raises(HarnessError, match="money_guard_usage_unknown"):
            await old.attempt("generator", {"model": ALLOWED_MODEL}, 1)
        await old.close()

    asyncio.run(seed())
    before_experiments = query(path, "SELECT * FROM money_guard_experiments")
    before_attempts = query(path, "SELECT * FROM money_guard_attempts")
    wrapper = VersionedMoneyGuardHTTPAttemptClient(
        BusinessClient({}), path, "report-evidence-v1-Q-CNY100",
    )
    original = read_billing_contract(path, "report-evidence-v1-Q-CNY100")
    previous_role = original["contracts"]["generator"]
    revised = replace(previous_role, endpoint_digest="new-explicit-binding", quote_cny=Decimal("30"))
    with pytest.raises(MoneyGuardConfigurationError, match="already_registered"):
        wrapper.register_contract(revised, original["billing_contract_digest"])
    with pytest.raises(MoneyGuardConfigurationError, match="role_digest_mismatch"):
        wrapper.register_contract(
            revised, original["billing_contract_digest"], expected_previous_role_digest="0" * 64,
        )
    wrapper.register_contract(
        revised, original["billing_contract_digest"],
        expected_previous_role_digest=previous_role.digest,
    )
    current = read_billing_contract(path, "report-evidence-v1-Q-CNY100")
    assert current["billing_contract_digest"] != original["billing_contract_digest"]
    assert current["contracts"]["generator"] == revised
    history = query(
        path, "SELECT role_digest,quote_micro FROM money_guard_contracts "
        "WHERE role='generator' ORDER BY contract_version",
    )
    assert history == [(previous_role.digest, 24_000_000), (revised.digest, 30_000_000)]
    assert query(path, "SELECT * FROM money_guard_experiments") == before_experiments
    assert query(path, "SELECT * FROM money_guard_attempts") == before_attempts

    async def blocked():
        with pytest.raises(MoneyGuardError, match="unknown_cost_ack_required"):
            await wrapper.attempt("generator", {"model": ALLOWED_MODEL}, 1)
        await wrapper.close()

    asyncio.run(blocked())

    # 合成旧进程记录缺少版本列的情形；补标必须指回旧合同，不能套用新报价。
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE money_guard_attempts SET contract_version=1,contract_digest='',"
            "contract_quote_micro=0",
        )
    reopened = VersionedMoneyGuardHTTPAttemptClient(
        BusinessClient({}), path, "report-evidence-v1-Q-CNY100",
    )
    assert query(path, "SELECT contract_version,contract_quote_micro,state,committed_micro "
                 "FROM money_guard_attempts") == [(1, 24_000_000, "unknown", 24_000_000)]
    asyncio.run(reopened.close())


def test_legacy_client_cannot_dispatch_under_a_revised_role_contract(tmp_path):
    path = tmp_path / "money.sqlite3"
    old, underlying = seed_legacy(path)
    wrapper = VersionedMoneyGuardHTTPAttemptClient(
        BusinessClient({}), path, "report-evidence-v1-Q-CNY100",
    )
    previous = read_billing_contract(path, "report-evidence-v1-Q-CNY100")
    role = previous["contracts"]["generator"]
    wrapper.register_contract(
        replace(role, endpoint_digest="explicit-new-endpoint"),
        previous["billing_contract_digest"], expected_previous_role_digest=role.digest,
    )

    async def run():
        with pytest.raises(MoneyGuardConfigurationError, match="contract_configuration_changed"):
            await old.attempt("generator", {"model": ALLOWED_MODEL}, 1)
        await old.close()
        await wrapper.close()

    asyncio.run(run())
    assert underlying.calls == []
    assert query(path, "SELECT count(*) FROM money_guard_attempts") == [(0,)]


@pytest.mark.parametrize("changes", [
    {"billing_mode": "local_token_free"},
    {"usage_source": "total_tokens"},
])
def test_reopened_legacy_client_rejects_changed_billing_semantics(tmp_path, changes):
    path = tmp_path / "money.sqlite3"
    old, _ = seed_legacy(path)
    asyncio.run(old.close())
    wrapper = VersionedMoneyGuardHTTPAttemptClient(
        BusinessClient({}), path, "report-evidence-v1-Q-CNY100",
    )
    previous = read_billing_contract(path, "report-evidence-v1-Q-CNY100")
    role = previous["contracts"]["generator"]
    wrapper.register_contract(
        replace(role, **changes), previous["billing_contract_digest"],
        expected_previous_role_digest=role.digest,
    )
    reopened, underlying = seed_legacy(path)

    async def run():
        try:
            with pytest.raises(MoneyGuardConfigurationError, match="contract_configuration_changed"):
                await reopened.attempt("generator", {"model": ALLOWED_MODEL}, 1)
        finally:
            await reopened.close()
            await wrapper.close()

    asyncio.run(run())
    assert underlying.calls == []
    assert query(path, "SELECT count(*) FROM money_guard_attempts") == [(0,)]


def test_settled_legacy_amount_is_unchanged_by_contract_revision_and_reopening(tmp_path):
    path = tmp_path / "money.sqlite3"
    old, _ = seed_legacy(path, response={"usage": {"cost": "0.1"}})

    async def settle():
        await old.attempt("generator", {"model": ALLOWED_MODEL}, 1)
        await old.close()

    asyncio.run(settle())
    before = query(path, "SELECT * FROM money_guard_attempts")
    assert query(path, "SELECT committed_micro,actual_micro,state FROM money_guard_attempts") == [
        (700_000, 700_000, "settled"),
    ]
    wrapper = VersionedMoneyGuardHTTPAttemptClient(
        BusinessClient({}), path, "report-evidence-v1-Q-CNY100",
    )
    previous = read_billing_contract(path, "report-evidence-v1-Q-CNY100")
    role = previous["contracts"]["generator"]
    wrapper.register_contract(
        replace(role, quote_cny=Decimal("30")), previous["billing_contract_digest"],
        expected_previous_role_digest=role.digest,
    )
    asyncio.run(wrapper.close())
    reopened = VersionedMoneyGuardHTTPAttemptClient(
        BusinessClient({}), path, "report-evidence-v1-Q-CNY100",
    )
    asyncio.run(reopened.close())
    assert query(path, "SELECT * FROM money_guard_attempts") == before


def test_v1_unknown_24_is_bound_to_v1_and_blocks_until_explicit_ack(tmp_path):
    path = tmp_path / "money.sqlite3"
    old, old_client = seed_legacy(
        path,
        cost="24",
        response={"output": "unknown"},
    )

    async def seed():
        with pytest.raises(HarnessError, match="money_guard_usage_unknown"):
            await old.attempt("generator", {"model": ALLOWED_MODEL}, 1)
        await old.close()

    asyncio.run(seed())
    before = query(
        path,
        """
        SELECT budget_micro, cost_upper_micro, reserved_micro, committed_micro,
               actual_micro, state, error_code
        FROM money_guard_experiments JOIN money_guard_attempts USING (experiment_id)
        """,
    )
    assert before == [(100_000_000, 24_000_000, 24_000_000, 24_000_000,
                       None, "unknown", "money_guard_usage_unknown")]
    summary_before = query(
        path,
        "SELECT budget_micro, cost_upper_micro, usd_to_cny_upper, model_digest, "
        "endpoint_digest, price_digest FROM money_guard_experiments",
    )

    client = BusinessClient({"expert": {"usage": {"total_tokens": 11}}})
    guard = VersionedMoneyGuardHTTPAttemptClient(
        client, path, "report-evidence-v1-Q-CNY100",
    )
    assert guard.registered_roles == ("generator", "reviewer")
    assert query(
        path,
        "SELECT contract_version, contract_quote_micro, state, committed_micro "
        "FROM money_guard_attempts",
    ) == [(1, 24_000_000, "unknown", 24_000_000)]

    expert = contract("expert", "fake/expert", "24", "local_token_free", "total_tokens")
    previous = guard.billing_contract_digest
    with pytest.raises(MoneyGuardConfigurationError, match="digest_mismatch"):
        guard.register_contract(expert, "0" * 64)
    guard.register_contract(expert, previous)
    assert guard.registered_roles == ("expert", "generator", "reviewer")

    async def blocked():
        with pytest.raises(MoneyGuardNotSent, match="unknown_cost_ack_required"):
            await guard.attempt("expert", {"model": "fake/expert"}, 1)

    asyncio.run(blocked())
    assert client.calls == []
    attempt_id = query(path, "SELECT attempt_id FROM money_guard_attempts")[0][0]
    guard.acknowledge_unknown_attempts([attempt_id])

    async def run():
        return await guard.attempt("expert", {"model": "fake/expert"}, 1)

    assert asyncio.run(run())["usage"]["total_tokens"] == 11
    after = query(
        path,
        """
        SELECT budget_micro, cost_upper_micro, reserved_micro, committed_micro,
               actual_micro, state, error_code
        FROM money_guard_experiments JOIN money_guard_attempts USING (experiment_id)
        ORDER BY attempt_id
        """,
    )
    assert after[0] == before[0]
    assert query(
        path,
        "SELECT budget_micro, cost_upper_micro, usd_to_cny_upper, model_digest, "
        "endpoint_digest, price_digest FROM money_guard_experiments",
    ) == summary_before
    assert after[1][2:6] == (24_000_000, 0, 0, "settled")
    assert len(old_client.calls) == 1


def test_new_wrapper_rejects_new_experiment_and_unregistered_contract(tmp_path):
    path = tmp_path / "money.sqlite3"
    old, _ = seed_legacy(path)
    asyncio.run(old.close())
    client = BusinessClient({})
    expert = contract("expert", "fake/expert", "24", "local_token_free", "total_tokens")

    with pytest.raises(MoneyGuardConfigurationError, match="ledger_missing"):
        VersionedMoneyGuardHTTPAttemptClient(client, path, "new-experiment")
    with pytest.raises(MoneyGuardConfigurationError, match="contract_unregistered"):
        VersionedMoneyGuardHTTPAttemptClient(
            client,
            path,
            "report-evidence-v1-Q-CNY100",
            contracts={"expert": expert},
        )


def test_role_quotes_fake_model_expert_usage_and_remote_cost(tmp_path):
    path = tmp_path / "money.sqlite3"
    old, _ = seed_legacy(path, cost="20")
    asyncio.run(old.close())
    client = BusinessClient({
        "expert": {"usage": {"total_tokens": 5}},
        "embedding": {"usage": {"cost": "1"}},
        "generator": {"usage": {"cost": "0.5"}},
    })
    guard = VersionedMoneyGuardHTTPAttemptClient(
        client, path, "report-evidence-v1-Q-CNY100",
    )
    digest = guard.billing_contract_digest
    guard.register_contract(
        contract("expert", "fake/expert", "24", "local_token_free", "total_tokens"),
        digest,
    )
    guard.register_contract(
        contract("embedding", "fake/embedding", "20", "remote_actual", "cost_usd"),
        guard.billing_contract_digest,
    )

    async def run():
        expert_result = await guard.attempt("expert", {"model": "fake/expert"}, 1)
        embedding_result = await guard.attempt("embedding", {"model": "fake/embedding"}, 1)
        generator_result = await guard.attempt(
            "generator", {"model": ALLOWED_MODEL}, 1,
        )
        return expert_result, embedding_result, generator_result

    expert_result, embedding_result, generator_result = asyncio.run(run())
    assert expert_result["usage"]["total_tokens"] == 5
    assert embedding_result["usage"]["cost"] == "1"
    assert generator_result["usage"]["cost"] == "0.5"
    attempts = query(
        path,
        "SELECT role, reserved_micro, committed_micro, actual_micro, "
        "contract_version, contract_digest, usage_total_tokens, usage_cost_usd "
        "FROM money_guard_attempts ORDER BY attempt_id",
    )
    assert [item[0] for item in attempts] == ["expert", "embedding", "generator"]
    assert attempts[0][1:4] == (24_000_000, 0, 0)
    assert attempts[0][6:] == (5, None)
    assert attempts[1][1:4] == (20_000_000, 7_000_000, 7_000_000)
    assert attempts[1][6:] == (None, "1")
    assert attempts[2][1:4] == (20_000_000, 3_500_000, 3_500_000)
    assert all(item[4] >= 1 and item[5] == guard.billing_contract_digest or item[0] == "expert"
               for item in attempts)
    with pytest.raises(MoneyGuardError, match="model_forbidden"):
        asyncio.run(guard.attempt("expert", {"model": "wrong/model"}, 1))


def test_remote_missing_cost_and_expert_missing_total_tokens_remain_unknown(tmp_path):
    path = tmp_path / "money.sqlite3"
    old, _ = seed_legacy(path, cost="10")
    asyncio.run(old.close())
    client = BusinessClient({
        "expert": {"usage": {"cost": "0"}},
        "embedding": {"usage": {}},
    })
    guard = VersionedMoneyGuardHTTPAttemptClient(
        client, path, "report-evidence-v1-Q-CNY100",
    )
    guard.register_contract(
        contract("expert", "fake/expert", "10", "local_token_free", "total_tokens"),
        guard.billing_contract_digest,
    )
    guard.register_contract(
        contract("embedding", "fake/embedding", "10", "remote_actual", "cost_usd"),
        guard.billing_contract_digest,
    )

    async def run():
        with pytest.raises(MoneyGuardError, match="usage_unknown"):
            await guard.attempt("expert", {"model": "fake/expert"}, 1)

    asyncio.run(run())
    unknown_id = query(
        path,
        "SELECT attempt_id FROM money_guard_attempts WHERE role='expert'",
    )[0][0]
    guard.acknowledge_unknown_attempts([unknown_id])

    async def run_embedding():
        with pytest.raises(MoneyGuardError, match="usage_unknown"):
            await guard.attempt("embedding", {"model": "fake/embedding"}, 1)

    asyncio.run(run_embedding())
    assert query(
        path,
        "SELECT state, committed_micro FROM money_guard_attempts "
        "WHERE role='embedding'",
    ) == [("unknown", 10_000_000)]


def test_concurrent_reservations_share_original_budget(tmp_path):
    path = tmp_path / "money.sqlite3"
    old, _ = seed_legacy(path, cost="60")
    asyncio.run(old.close())
    client = BlockingBusinessClient()
    guard = VersionedMoneyGuardHTTPAttemptClient(
        client, path, "report-evidence-v1-Q-CNY100",
    )

    async def run():
        first = asyncio.create_task(
            guard.attempt("generator", {"model": ALLOWED_MODEL}, 1),
        )
        await client.started.wait()
        second = asyncio.create_task(
            guard.attempt("reviewer", {"model": ALLOWED_MODEL}, 1),
        )
        await asyncio.sleep(0.02)
        client.release.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    first_result, second_result = asyncio.run(run())
    assert not isinstance(first_result, BaseException)
    assert isinstance(second_result, MoneyGuardError)
    assert second_result.code == "money_budget_exhausted"
    assert len(client.calls) == 1
    assert query(
        path,
        "SELECT SUM(committed_micro) FROM money_guard_attempts",
    ) == [(0,)]


def test_close_delegates_to_underlying_client(tmp_path):
    path = tmp_path / "money.sqlite3"
    old, _ = seed_legacy(path)
    asyncio.run(old.close())
    client = BusinessClient({})
    guard = VersionedMoneyGuardHTTPAttemptClient(
        client, path, "report-evidence-v1-Q-CNY100",
    )
    asyncio.run(guard.close())
    asyncio.run(guard.close())
    assert client.close_calls == 1


def test_read_billing_contract_is_ro_and_returns_registered_roles(tmp_path):
    path = tmp_path / "money.sqlite3"
    old, _ = seed_legacy(path, cost="20")
    asyncio.run(old.close())
    client = BusinessClient({})
    guard = VersionedMoneyGuardHTTPAttemptClient(
        client, path, "report-evidence-v1-Q-CNY100",
    )
    guard.register_contract(
        contract("expert", "fake/expert", "24", "local_token_free", "total_tokens"),
        guard.billing_contract_digest,
    )

    snapshot = read_billing_contract(path, "report-evidence-v1-Q-CNY100")
    assert set(snapshot) == {
        "billing_contract_digest", "contracts", "usd_to_cny_upper",
    }
    assert snapshot["billing_contract_digest"] == guard.billing_contract_digest
    assert tuple(sorted(snapshot["contracts"])) == (
        "expert", "generator", "reviewer",
    )
    assert snapshot["usd_to_cny_upper"] == Decimal("7")
    assert snapshot["contracts"]["expert"].endpoint_digest == "endpoint-expert"
    snapshot["contracts"]["other"] = snapshot["contracts"]["expert"]
    assert "other" in snapshot["contracts"]


def test_read_billing_contract_does_not_initialize_missing_version_contracts(tmp_path):
    path = tmp_path / "without-contracts.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE money_guard_experiments("
            "experiment_id TEXT PRIMARY KEY, usd_to_cny_upper TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO money_guard_experiments VALUES (?, ?)",
            ("report-evidence-v1-Q-CNY100", "7"),
        )

    with pytest.raises(MoneyGuardConfigurationError, match="contracts_missing"):
        read_billing_contract(path, "report-evidence-v1-Q-CNY100")
    with sqlite3.connect(path) as connection:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert tables == {"money_guard_experiments"}
