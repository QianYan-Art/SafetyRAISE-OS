import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.money_guard import MoneyGuardNotSent
from app.report_harness.request_ledger import RequestLedger
from app.report_harness.transport import BudgetedTransport, HTTPAttemptClient, RequestBound
from app.schemas.report_run import BudgetPolicy, CreateRunRequest
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles, dependencies


@contextmanager
def physical_server(responses):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            status, body = responses.pop(0)
            if callable(body):
                body = body(calls[-1])
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if status == 302:
                self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/synthetic", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def budget_run(pg_store, policy=None):
    store, owner, _, session = pg_store
    config = dependencies(SyntheticRoles())
    service = ReportRunService(store, config)
    run = service.create(owner, CreateRunRequest(
        request_id=uuid4(), session_id=session, evidence_revision=0,
        accident_data={"事实": "合成 HTTP 预算样本"},
    ))
    with store.locked(owner, run["run_id"]) as (conn, row):
        updated = store.save(conn, row, "queued", {
            **row["document"], "budget_policy": (policy or BudgetPolicy()).model_dump(mode="json"),
        }, "checkpoint", {"step": "synthetic_budget"})
    token = service.claim(owner, run["run_id"], updated["state_version"])
    return service, RequestLedger(store), owner, run["run_id"], token, config


def gateway(setup, url, *, proof_registered=True, authorize=None, client=None):
    service, ledger, owner, run_id, token, config = setup
    proof = canonical_digest({"protocol": "fixed-synthetic-http-usage"})
    return BudgetedTransport(
        ledger, client or HTTPAttemptClient({role: url for role in (
            "generator", "reviewer", "expert", "embedding", "probe",
        )}),
        owner=owner, run_id=run_id, token=token,
        endpoint_digest=config.endpoint_profile_digest, output_limit=20, review_reserve_tokens=200,
        generation_reserve_tokens=200,
        authorize=authorize or (lambda: service.store.assert_active(owner, run_id, token)),
        remaining_seconds=lambda: 5,
        bound_provider=lambda role, payload: RequestBound(200, 20, proof),
        verified_proofs=frozenset({proof}) if proof_registered else frozenset(),
    )


def test_payload_output_limit_must_match_verified_bound(pg_store):
    setup = budget_run(pg_store)
    with physical_server([]) as (url, calls):
        async def execute():
            transport = gateway(setup, url)
            try:
                with pytest.raises(HarnessError, match="token_bound_unverified"):
                    await transport.request(
                        "generator", {"max_tokens": 21}, output_limit_field="max_tokens",
                    )
            finally:
                await transport.close()
        asyncio.run(execute())
        assert calls == []
    assert setup[1].view(setup[2], setup[3])["physical_requests"] == 0


def test_real_http_attempts_are_individually_reserved_and_settled(pg_store):
    setup = budget_run(pg_store)
    with physical_server([
        (200, {"output": "合成生成", "usage": {"total_tokens": 17}}),
        (200, {"output": "合成审查", "usage": {"total_tokens": 11}}),
    ]) as (url, calls):
        async def execute():
            transport = gateway(setup, url)
            try:
                await transport.request("generator", {"synthetic": "generate"})
                await transport.request("reviewer", {"synthetic": "review"})
            finally:
                await transport.close()

        asyncio.run(execute())
        assert len(calls) == 2
    _, ledger, owner, run_id, _, _ = setup
    view = ledger.view(owner, run_id)
    assert view["physical_requests"] == 2
    assert view["known_used"] == 28
    assert view["unknown_reserved"] == view["inflight_reserved"] == 0
    assert view["remaining"] == 120000 - 28


def test_missing_usage_stops_next_http_without_claiming_zero_cost(pg_store):
    setup = budget_run(pg_store)
    with physical_server([(200, {"output": "没有 usage"})]) as (url, calls):
        async def execute():
            transport = gateway(setup, url)
            try:
                with pytest.raises(HarnessError, match="usage_unknown"):
                    await transport.request("generator", {"synthetic": True})
                with pytest.raises(HarnessError):
                    await transport.request("reviewer", {"synthetic": True})
            finally:
                await transport.close()

        asyncio.run(execute())
        assert len(calls) == 1
    _, ledger, owner, run_id, _, _ = setup
    view = ledger.view(owner, run_id)
    assert view["known_used"] == 0
    assert view["unknown_reserved"] == 200
    assert view["remaining"] == 120000 - 200


def test_money_guard_pre_send_denial_releases_report_reservation(pg_store):
    setup = budget_run(pg_store)

    class LocalDenial:
        registered_roles = ("generator", "reviewer", "expert", "embedding", "probe")

        async def attempt(self, role, payload, timeout):
            raise MoneyGuardNotSent("unknown_cost_ack_required")

        async def close(self):
            pass

    async def execute():
        transport = gateway(setup, "http://unused.invalid", client=LocalDenial())
        try:
            with pytest.raises(MoneyGuardNotSent, match="unknown_cost_ack_required"):
                await transport.request("expert", {"synthetic": True})
        finally:
            await transport.close()

    asyncio.run(execute())
    _, ledger, owner, run_id, _, _ = setup
    view = ledger.view(owner, run_id)
    assert view["physical_requests"] == 0
    assert view["unknown_reserved"] == view["inflight_reserved"] == 0
    assert view["known_used"] == 0


@pytest.mark.parametrize("status", [302, 500])
def test_http_failure_does_not_hide_retries_or_follow_redirects(pg_store, status):
    setup = budget_run(pg_store)
    with physical_server([(status, {"error": "合成失败"})]) as (url, calls):
        async def execute():
            transport = gateway(setup, url)
            try:
                with pytest.raises(HarnessError, match="completion_unknown"):
                    await transport.request("expert", {"synthetic": True})
            finally:
                await transport.close()

        asyncio.run(execute())
        assert len(calls) == 1
    _, ledger, owner, run_id, _, _ = setup
    assert ledger.view(owner, run_id)["unknown_reserved"] == 200


def test_unverified_bound_never_registers_or_sends_request(pg_store):
    setup = budget_run(pg_store)
    with physical_server([]) as (url, calls):
        async def execute():
            transport = gateway(setup, url, proof_registered=False)
            try:
                with pytest.raises(HarnessError, match="token_bound_unverified"):
                    await transport.request("generator", {"synthetic": True})
            finally:
                await transport.close()

        asyncio.run(execute())
        assert calls == []
    _, ledger, owner, run_id, _, _ = setup
    assert ledger.view(owner, run_id)["physical_requests"] == 0


def test_cancel_between_reservation_and_dispatch_prevents_http(pg_store):
    setup = budget_run(pg_store)
    service, ledger, owner, run_id, token, _ = setup
    checkpoints = 0

    def authorize():
        nonlocal checkpoints
        checkpoints += 1
        if checkpoints == 2:
            service.cancel(owner, run_id)
        service.store.assert_active(owner, run_id, token)

    with physical_server([]) as (url, calls):
        async def execute():
            transport = gateway(setup, url, authorize=authorize)
            try:
                with pytest.raises(HarnessError, match="lease_lost"):
                    await transport.request("generator", {"synthetic": True})
            finally:
                await transport.close()

        asyncio.run(execute())
        assert calls == []
    assert ledger.view(owner, run_id)["physical_requests"] == 0
    assert ledger.view(owner, run_id)["remaining"] == 120000
