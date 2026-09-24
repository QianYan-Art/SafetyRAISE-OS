from __future__ import annotations

import asyncio
import os
import sqlite3
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.money_guard import MoneyGuardNotSent
from evals.report_harness.money_guard import (
    ALLOWED_MODEL,
    MoneyGuardConfigurationError,
    MoneyGuardError,
    MoneyGuardHTTPAttemptClient,
)


class SyntheticClient:
    registered_roles = ("generator", "reviewer")

    def __init__(self, response=None, error: BaseException | None = None):
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict, float]] = []
        self.close_calls = 0

    async def attempt(self, role, payload, timeout):
        self.calls.append((role, payload, timeout))
        if self.error is not None:
            raise self.error
        if callable(self.response):
            return self.response()
        return self.response

    async def close(self):
        self.close_calls += 1


class BlockingClient(SyntheticClient):
    def __init__(self, response):
        super().__init__(response=response)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def attempt(self, role, payload, timeout):
        self.calls.append((role, payload, timeout))
        self.started.set()
        await self.release.wait()
        return self.response


def make_guard(path: Path, client, *, experiment_id="line-10", cost="10", rate="7", **kwargs):
    return MoneyGuardHTTPAttemptClient(
        client,
        path,
        experiment_id,
        Decimal(cost),
        Decimal(rate),
        **kwargs,
    )


def payload(model=ALLOWED_MODEL):
    return {"model": model, "messages": [{"role": "user", "content": "合成"}]}


def rows(path: Path):
    with sqlite3.connect(path) as connection:
        experiment = connection.execute(
            "SELECT budget_micro, blocked_reason FROM money_guard_experiments"
        ).fetchone()
        attempts = connection.execute(
            """
            SELECT reserved_micro, committed_micro, actual_micro, state, error_code
            FROM money_guard_attempts
            ORDER BY attempt_id
            """
        ).fetchall()
    return experiment, attempts


def test_success_settles_upward_in_cny_micro_units_and_close_is_idempotent(tmp_path):
    path = tmp_path / "money.sqlite3"
    client = SyntheticClient({"usage": {"cost": "0.0000011"}, "output": "合成"})
    guard = make_guard(path, client, cost="1")

    async def run():
        response = await guard.attempt("generator", payload(), 3.5)
        await guard.close()
        await guard.close()
        return response

    assert asyncio.run(run())["output"] == "合成"
    assert len(client.calls) == 1
    assert client.close_calls == 1
    experiment, attempts = rows(path)
    assert experiment == (100_000_000, None)
    assert attempts == [(1_000_000, 8, 8, "settled", None)]


def test_exception_keeps_reservation_without_an_implicit_retry(tmp_path):
    path = tmp_path / "money.sqlite3"
    client = SyntheticClient(error=RuntimeError("synthetic failure"))
    guard = make_guard(path, client, cost="40")

    async def run():
        with pytest.raises(RuntimeError, match="synthetic failure"):
            await guard.attempt("reviewer", payload(), 1)

    asyncio.run(run())
    assert len(client.calls) == 1
    _, attempts = rows(path)
    assert attempts == [(40_000_000, 40_000_000, None, "unknown", "underlying_attempt_failed")]


def test_budget_rejection_is_confirmed_before_underlying_client(tmp_path):
    path = tmp_path / "money.sqlite3"
    client = SyntheticClient({"usage": {"cost": "8"}, "output": "first"})
    guard = make_guard(path, client, cost="60")

    async def run():
        assert (await guard.attempt("generator", payload(), 1))["output"] == "first"
        with pytest.raises(MoneyGuardNotSent, match="money_budget_exhausted"):
            await guard.attempt("generator", payload(), 1)

    asyncio.run(run())
    assert len(client.calls) == 1
    assert rows(path)[1] == [(60_000_000, 56_000_000, 56_000_000, "settled", None)]


def test_missing_usage_cost_keeps_reservation_and_consumes_budget_for_next_instance(tmp_path):
    path = tmp_path / "money.sqlite3"
    first_client = SyntheticClient({"output": "没有费用"})
    first = make_guard(path, first_client, cost="60")

    async def first_run():
        with pytest.raises(HarnessError, match="money_guard_usage_unknown"):
            await first.attempt("generator", payload(), 1)

    asyncio.run(first_run())
    second_client = SyntheticClient({"usage": {"cost": "0"}})
    second = make_guard(path, second_client, cost="60")

    async def second_run():
        with pytest.raises(HarnessError, match="money_budget_exhausted"):
            await second.attempt("reviewer", payload(), 1)

    asyncio.run(second_run())
    assert second_client.calls == []
    _, attempts = rows(path)
    assert attempts[0][1:4] == (60_000_000, None, "unknown")


@pytest.mark.parametrize("bad_cost", ["-0.1", "NaN", True])
def test_invalid_cost_blocks_future_requests_and_does_not_release_reservation(tmp_path, bad_cost):
    path = tmp_path / f"invalid-{str(bad_cost).replace('/', '_')}.sqlite3"
    client = SyntheticClient({"usage": {"cost": bad_cost}})
    guard = make_guard(path, client, cost="2")

    async def run():
        with pytest.raises(HarnessError, match="money_guard_invalid_usage_cost"):
            await guard.attempt("generator", payload(), 1)
        with pytest.raises(HarnessError, match="money_guard_blocked"):
            await guard.attempt("reviewer", payload(), 1)

    asyncio.run(run())
    assert len(client.calls) == 1
    experiment, attempts = rows(path)
    assert experiment == (100_000_000, "money_guard_invalid_usage_cost")
    assert attempts[0][0:4] == (2_000_000, 2_000_000, None, "blocked")


def test_billing_overrun_blocks_future_requests(tmp_path):
    path = tmp_path / "overrun.sqlite3"
    client = SyntheticClient({"usage": {"cost": "1"}})
    guard = make_guard(path, client, cost="1", rate="7")

    async def run():
        with pytest.raises(HarnessError, match="money_guard_billing_overrun"):
            await guard.attempt("generator", payload(), 1)
        with pytest.raises(HarnessError, match="money_guard_blocked"):
            await guard.attempt("reviewer", payload(), 1)

    asyncio.run(run())
    assert len(client.calls) == 1
    experiment, attempts = rows(path)
    assert experiment == (100_000_000, "money_guard_billing_overrun")
    assert attempts[0][0:4] == (1_000_000, 1_000_000, 7_000_000, "blocked")


def test_same_ledger_concurrent_reservations_cannot_exceed_budget(tmp_path):
    path = tmp_path / "concurrent.sqlite3"
    first_client = BlockingClient({"usage": {"cost": "0"}})
    second_client = SyntheticClient({"usage": {"cost": "0"}})
    first = make_guard(path, first_client, cost="60")
    second = make_guard(path, second_client, cost="60")

    async def run():
        first_task = asyncio.create_task(first.attempt("generator", payload(), 1))
        await first_client.started.wait()
        second_task = asyncio.create_task(second.attempt("reviewer", payload(), 1))
        await asyncio.sleep(0.05)
        first_client.release.set()
        return await asyncio.gather(first_task, second_task, return_exceptions=True)

    results = asyncio.run(run())
    first_result, second_result = results
    assert not isinstance(first_result, BaseException)
    assert first_result["usage"]["cost"] == "0"
    assert isinstance(second_result, HarnessError)
    assert second_result.code == "money_budget_exhausted"
    assert len(first_client.calls) == 1
    assert second_client.calls == []
    _, attempts = rows(path)
    assert len(attempts) == 1
    assert attempts[0][1] == 0


def test_restart_does_not_clear_unknown_reservation(tmp_path):
    path = tmp_path / "restart.sqlite3"
    first_client = SyntheticClient({"usage": {}})
    first = make_guard(path, first_client, cost="55")

    async def first_run():
        with pytest.raises(HarnessError, match="money_guard_usage_unknown"):
            await first.attempt("generator", payload(), 1)
        await first.close()

    asyncio.run(first_run())

    child = """
import asyncio
from evals.report_harness.money_guard import MoneyGuardError, MoneyGuardHTTPAttemptClient

class Client:
    registered_roles = (\"generator\", \"reviewer\")
    async def attempt(self, role, payload, timeout):
        return {\"usage\": {\"cost\": \"0\"}}
    async def close(self):
        pass

async def main():
    guard = MoneyGuardHTTPAttemptClient(Client(), r\"%s\", \"line-10\", \"55\", \"7\")
    try:
        try:
            await guard.attempt(\"reviewer\", {\"model\": \"%s\"}, 1)
        except MoneyGuardError as exc:
            assert exc.code == \"money_budget_exhausted\"
        else:
            raise AssertionError(\"restart must preserve the unknown reservation\")
    finally:
        await guard.close()

asyncio.run(main())
""" % (str(path), ALLOWED_MODEL)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1])
    completed = subprocess.run(
        [sys.executable, "-c", child],
        cwd=Path(__file__).parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    _, attempts = rows(path)
    assert len(attempts) == 1
    assert attempts[0][1:4] == (55_000_000, None, "unknown")


def test_runner_constructor_shape_accepts_path_and_profile(tmp_path):
    path = tmp_path / "runner-shape.sqlite3"
    client = SyntheticClient({"usage": {"cost": "0"}})
    guard = MoneyGuardHTTPAttemptClient(
        client=client,
        path=path,
        experiment_id="report-evidence-v1-Q-CNY100",
        profile={
            "provider": "tencent",
            "model": "hy4-preview",
            "endpoints": {"generator": "ctx1048576", "reviewer": "ctx1048576"},
            "context_limit": 1_048_576,
            "output_limit": 64_000,
        },
        cost_upper_cny=Decimal("13"),
        usd_to_cny_upper=Decimal("10"),
    )

    async def run():
        return await guard.attempt("generator", payload(), 1)

    assert asyncio.run(run())["usage"]["cost"] == "0"


@pytest.mark.parametrize(
    "changes",
    [
        {"cost": "11"},
        {"rate": "7.1"},
        {"endpoint_summary": {"generator": "changed"}},
        {"models": {"generator": ALLOWED_MODEL, "reviewer": "other/model"}},
    ],
)
def test_configuration_changes_are_rejected(tmp_path, changes):
    path = tmp_path / "config.sqlite3"
    first = make_guard(path, SyntheticClient({"usage": {"cost": "0"}}))
    assert first.registered_roles == ("generator", "reviewer")
    with pytest.raises((MoneyGuardConfigurationError, ValueError), match="money_guard_"):
        make_guard(path, SyntheticClient({"usage": {"cost": "0"}}), **changes)


def test_roles_and_models_are_strict_and_close_releases_client(tmp_path):
    class ExtraRoleClient(SyntheticClient):
        registered_roles = ("generator", "reviewer", "expert")

    with pytest.raises(MoneyGuardConfigurationError):
        make_guard(tmp_path / "extra.sqlite3", ExtraRoleClient())

    with pytest.raises(MoneyGuardConfigurationError):
        make_guard(
            tmp_path / "model.sqlite3",
            SyntheticClient(),
            models={"generator": ALLOWED_MODEL, "reviewer": "other/model"},
        )

    client = SyntheticClient({"usage": {"cost": "0"}})
    guard = make_guard(tmp_path / "close.sqlite3", client)

    async def run():
        await guard.close()
        with pytest.raises(HarnessError, match="money_guard_closed"):
            await guard.attempt("generator", payload(), 1)

    asyncio.run(run())
    assert client.close_calls == 1
