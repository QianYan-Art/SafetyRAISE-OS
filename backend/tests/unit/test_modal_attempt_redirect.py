import asyncio

import httpx
import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.transport import HTTPAttemptClient


def _client(handler):
    client = HTTPAttemptClient(
        {"expert": "https://safetyraise.modal.run/v1/chat/completions"},
        {"expert": {"Authorization": "Bearer test-token"}},
    )
    original = client._client
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        trust_env=False,
        follow_redirects=False,
    )
    return client, original


def test_modal_attempt_redirect_recovers_result_without_second_post():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                303,
                headers={
                    "Location": (
                        "https://safetyraise.modal.run/v1/chat/completions"
                        "?__modal_attempt_token=attempt-secret"
                    ),
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"total_tokens": 7},
            },
        )

    async def run():
        client, original = _client(handler)
        await original.aclose()
        try:
            result = await client.attempt("expert", {"model": "synthetic"}, 2)
            assert result["usage"]["total_tokens"] == 7
        finally:
            await client.close()

    asyncio.run(run())
    assert [request.method for request in requests] == ["POST", "GET"]
    assert dict(requests[1].url.params) == {
        "__modal_attempt_token": "attempt-secret",
    }
    assert all(request.headers["authorization"] == "Bearer test-token" for request in requests)


@pytest.mark.parametrize(
    "location",
    [
        "https://attacker.invalid/v1/chat/completions?__modal_attempt_token=secret",
        "https://safetyraise.modal.run/other?__modal_attempt_token=secret",
        "https://safetyraise.modal.run/v1/chat/completions?other=secret",
        (
            "https://safetyraise.modal.run/v1/chat/completions"
            "?__modal_attempt_token=secret&extra=1"
        ),
    ],
)
def test_modal_attempt_redirect_rejects_untrusted_location_without_get(location):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(303, headers={"Location": location})

    async def run():
        client, original = _client(handler)
        await original.aclose()
        try:
            with pytest.raises(HarnessError, match="physical_redirect_rejected") as exc:
                await client.attempt("expert", {"model": "synthetic"}, 2)
            assert "secret" not in str(exc.value)
        finally:
            await client.close()

    asyncio.run(run())
    assert [request.method for request in requests] == ["POST"]


def test_modal_attempt_retrieval_failure_does_not_leak_token_or_retry_post():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(
                303,
                headers={
                    "Location": (
                        "https://safetyraise.modal.run/v1/chat/completions"
                        "?__modal_attempt_token=attempt-secret"
                    ),
                },
            )
        return httpx.Response(503)

    async def run():
        client, original = _client(handler)
        await original.aclose()
        try:
            with pytest.raises(
                HarnessError,
                match="physical_attempt_retrieval_failed",
            ) as exc:
                await client.attempt("expert", {"model": "synthetic"}, 2)
            assert "attempt-secret" not in str(exc.value)
        finally:
            await client.close()

    asyncio.run(run())
    assert [request.method for request in requests] == ["POST", "GET"]
