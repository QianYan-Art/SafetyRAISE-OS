import asyncio
from dataclasses import replace
from decimal import Decimal
import sqlite3

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.money_guard import (
    MoneyGuardHTTPAttemptClient, VersionedMoneyGuardHTTPAttemptClient,
)
from evals.report_harness.development_provider import MODEL
from evals.report_harness.development_run import (
    development_billing_contracts, development_billing_state, execution_proof,
)

EXPERIMENT_ID = "report-evidence-v1-Q-CNY100"


class Client:
    registered_roles = ("generator", "reviewer")

    def __init__(self):
        self.calls = 0

    async def attempt(self, role, payload, timeout):
        self.calls += 1
        return {"usage": {"cost": "0.01"}}

    async def close(self):
        pass


def test_development_contract_requires_explicit_revision_and_keeps_unknown_reserve(tmp_path):
    path = tmp_path / "money.sqlite3"
    legacy = MoneyGuardHTTPAttemptClient(
        Client(), path, EXPERIMENT_ID, cost_upper_cny=24, usd_to_cny_upper=10,
        profile={"model": MODEL, "output_limit": 16384},
    )
    asyncio.run(legacy.close())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO money_guard_attempts "
            "(experiment_id,role,reserved_micro,committed_micro,state) "
            "VALUES ('report-evidence-v1-Q-CNY100','generator',24000000,24000000,'unknown')",
        )
    proof = execution_proof("合成供应商版本")
    with pytest.raises(HarnessError, match="contract_configuration_changed"):
        development_billing_state(path, proof)
    client = Client()
    guard = VersionedMoneyGuardHTTPAttemptClient(client, path, EXPERIMENT_ID)
    expected = development_billing_contracts(proof)
    for role, contract in expected.items():
        old = guard._contracts[role]
        guard.register_contract(
            contract, guard.billing_contract_digest, expected_previous_role_digest=old.digest,
        )
    assert development_billing_state(path, proof)["contracts"] == expected

    async def exercise():
        with pytest.raises(HarnessError, match="unknown_cost_ack_required"):
            await guard.attempt("generator", {"model": MODEL}, 1)
        assert client.calls == 0
        guard.acknowledge_unknown_attempts([1])
        await guard.attempt("generator", {"model": MODEL}, 1)
        await guard.close()

    asyncio.run(exercise())
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT state,committed_micro FROM money_guard_attempts ORDER BY attempt_id",
        ).fetchall()
    assert rows == [("unknown", 24000000), ("settled", 100000)]
    assert client.calls == 1


def test_development_contract_rejects_new_role_semantics(tmp_path):
    path = tmp_path / "money.sqlite3"
    legacy = MoneyGuardHTTPAttemptClient(
        Client(), path, EXPERIMENT_ID, cost_upper_cny=24, usd_to_cny_upper=10,
    )
    asyncio.run(legacy.close())
    guard = VersionedMoneyGuardHTTPAttemptClient(Client(), path, EXPERIMENT_ID)
    proof = execution_proof("合成版本")
    for role, contract in development_billing_contracts(proof).items():
        old = guard._contracts[role]
        guard.register_contract(
            replace(contract, quote_cny=Decimal("25")), guard.billing_contract_digest,
            expected_previous_role_digest=old.digest,
        )
    with pytest.raises(HarnessError, match="contract_configuration_changed"):
        development_billing_state(path, proof)
    asyncio.run(guard.close())
