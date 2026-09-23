import sqlite3
from dataclasses import replace
from decimal import Decimal

import pytest

from app.report_harness.money_guard import BUDGET_MICRO_CNY, RoleBillingContract, read_billing_contract
from app.report_harness.provision import create_experiment, main, register_contracts


def contracts():
    return {role: RoleBillingContract(
        role=role, model=f"synthetic-{role}", endpoint_digest=role[0] * 64,
        quote_cny=Decimal(0) if role == "expert" else Decimal("1.5"),
        billing_mode="local_token_free" if role == "expert" else "remote_actual",
        usage_source="self_hosted_usage" if role == "expert" else "cost_usd",
    ) for role in ("expert", "embedding", "generator", "reviewer")}


def test_new_experiment_registers_all_roles_once_and_only_appends_changes(tmp_path):
    ledger = tmp_path / "ledger" / "money.sqlite3"
    create_experiment(ledger, "demo")
    wanted = contracts()
    digest = register_contracts(ledger, "demo", wanted)
    state = read_billing_contract(ledger, "demo")
    assert state["contracts"] == wanted and state["billing_contract_digest"] == digest
    assert register_contracts(ledger, "demo", wanted) == digest

    changed = {**wanted, "generator": replace(wanted["generator"], quote_cny=Decimal("2"))}
    assert register_contracts(ledger, "demo", changed) != digest
    with sqlite3.connect(ledger) as conn:
        budget = conn.execute("SELECT budget_micro FROM money_guard_experiments").fetchone()[0]
        versions = conn.execute("SELECT count(*) FROM money_guard_contract_versions").fetchone()[0]
        attempts = conn.execute("SELECT count(*) FROM money_guard_attempts").fetchone()[0]
    assert budget == BUDGET_MICRO_CNY
    # 初版两角色 + 首次登记四角色 + 修订一个角色；没有任何请求记录。
    assert versions == 1 + 4 + 1 and attempts == 0


def test_cli_requires_explicit_creation_and_never_recreates(tmp_path, monkeypatch):
    import app.report_harness.provision as provision

    ledger = tmp_path / "money.sqlite3"
    metadata = tmp_path / "metadata.json"
    metadata.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(provision, "load_settings", lambda _config: object())
    base = ["--metadata", str(metadata), "--ledger", str(ledger), "--experiment", "demo",
            "--output", str(tmp_path / "manifest.json")]
    with pytest.raises(SystemExit):
        main(base)
    create_experiment(ledger, "demo")
    with pytest.raises(SystemExit):
        main([*base, "--create-experiment"])
