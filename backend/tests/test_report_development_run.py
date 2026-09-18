import hashlib
import json
import asyncio

import pytest

from evals.report_harness.development_run import execution_proof, load_input
from evals.report_harness.development_provider import REQUEST_CNY_UPPER, USD_TO_CNY_UPPER
from evals.report_harness.money_guard import MoneyGuard


def test_development_input_is_digest_bound(tmp_path):
    path = tmp_path / "input.json"
    raw = json.dumps({"事实": "合成匿名材料"}, ensure_ascii=False).encode()
    path.write_bytes(raw)
    assert load_input(path, hashlib.sha256(raw).hexdigest()) == {"事实": "合成匿名材料"}
    with pytest.raises(ValueError):
        load_input(path, "0" * 64)


@pytest.mark.parametrize("value", [{}, [], {"事实": 1}, {"事实": {"嵌套": "不允许"}}])
def test_development_input_rejects_unapproved_shape(tmp_path, value):
    path = tmp_path / "input.json"
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        load_input(path, hashlib.sha256(raw).hexdigest())


def test_actual_runner_profile_constructs_persistent_guard(tmp_path):
    class Client:
        registered_roles = ("generator", "reviewer")

        async def close(self):
            pass

    guard = MoneyGuard(
        client=Client(), path=tmp_path / "money.sqlite3",
        experiment_id="report-evidence-v1-Q-CNY100",
        profile=execution_proof("合成版本"), cost_upper_cny=REQUEST_CNY_UPPER,
        usd_to_cny_upper=USD_TO_CNY_UPPER,
    )
    assert guard.registered_roles == ("generator", "reviewer")
    asyncio.run(guard.close())
