from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

import httpx

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError


@dataclass(frozen=True)
class RequestBound:
    """由服务端经验证的协议提供；不是按字符数猜测的 token 数。"""

    total_tokens: int
    output_tokens: int
    proof_digest: str

    def validate(self, output_limit: int) -> None:
        if (type(self.total_tokens) is not int or type(self.output_tokens) is not int
                or not 0 <= self.output_tokens <= min(self.total_tokens, output_limit)
                or not isinstance(self.proof_digest, str) or len(self.proof_digest) != 64
                or any(char not in "0123456789abcdef" for char in self.proof_digest)):
            raise HarnessError("token_bound_unverified")


class HTTPAttemptClient:
    """每次调用只有一个 HTTP attempt；无自动重试、重定向、代理或端点回退。"""

    def __init__(self, endpoints: dict[str, str], headers: dict[str, dict] | None = None):
        self._endpoints = deepcopy(endpoints)
        self._headers = deepcopy(headers or {})
        for address in self._endpoints.values():
            url = urlsplit(address)
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username or url.password or url.fragment or url.query):
                raise ValueError("请求端点必须是服务端登记的无内嵌凭据地址。")
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(retries=0), trust_env=False,
            follow_redirects=False,
        )

    async def attempt(self, role: str, payload: dict, timeout: float) -> dict:
        if role not in self._endpoints:
            raise HarnessError("endpoint_role_unregistered")
        async with self._client.stream(
            "POST", self._endpoints[role], json=payload,
            headers=self._headers.get(role, {}), timeout=timeout,
        ) as response:
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise HarnessError("physical_response_too_large")
        def reject_nonfinite(value):
            raise HarnessError("invalid_physical_response")

        result = json.loads(content, parse_constant=reject_nonfinite)
        if not isinstance(result, dict):
            raise HarnessError("invalid_physical_response")
        return result

    async def close(self):
        await self._client.aclose()

    @property
    def registered_roles(self) -> tuple[str, ...]:
        return tuple(sorted(self._endpoints))


def reported_tokens(response: dict) -> int | None:
    """没有可核对的 usage 就保持未知，不把空值或布尔值解释为零。"""
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if type(total) is int and total >= 0:
        return total
    return None


class BudgetedTransport:
    """每个物理 attempt 先持久登记，结算与终态正文权限分开。"""

    def __init__(self, ledger, client, *, owner: str, run_id: str, token: int,
                 endpoint_digest: str, output_limit: int, review_reserve_tokens: int,
                 generation_reserve_tokens: int,
                 authorize: Callable[[], None], remaining_seconds: Callable[[], float],
                 bound_provider: Callable[[str, dict], RequestBound],
                 verified_proofs: frozenset[str]):
        self.ledger, self.client = ledger, client
        self.owner, self.run_id, self.token = owner, run_id, token
        if type(output_limit) is not int or output_limit <= 0:
            raise ValueError("输出上限必须是正整数。")
        policy = ledger.policy(owner, run_id, token)
        self.endpoint_digest = endpoint_digest
        self.output_limit = min(output_limit, policy.max_output_tokens_per_request)
        self.review_reserve_tokens = review_reserve_tokens
        self.generation_reserve_tokens = generation_reserve_tokens
        self.authorize, self.remaining_seconds = authorize, remaining_seconds
        self.bound_provider, self.verified_proofs = bound_provider, frozenset(verified_proofs)
        ledger.bind_runtime_profile(owner, run_id, token, {
            "generation_reserve_tokens": generation_reserve_tokens,
            "review_reserve_tokens": review_reserve_tokens,
            "output_limit": self.output_limit,
            "endpoint_digest": endpoint_digest,
            "proofs": sorted(self.verified_proofs),
            "roles": list(client.registered_roles),
        })

    def preflight_initial(self) -> None:
        self.authorize()
        self.ledger.ensure_capacity(
            self.owner, self.run_id, self.token, requests=2,
            tokens=self.generation_reserve_tokens + self.review_reserve_tokens,
        )

    def _bound(self, role: str, payload: dict, output_limit_field: str | None) -> RequestBound:
        bound = self.bound_provider(role, deepcopy(payload))
        if not isinstance(bound, RequestBound) or bound.proof_digest not in self.verified_proofs:
            raise HarnessError("token_bound_unverified")
        bound.validate(self.output_limit)
        if output_limit_field is not None and (
            output_limit_field not in {"max_tokens", "max_completion_tokens", "max_output_tokens"}
            or type(payload.get(output_limit_field)) is not int
            or payload[output_limit_field] != bound.output_tokens
        ):
            raise HarnessError("token_bound_unverified")
        if role in {"expert", "generator", "reviewer"} and bound.output_tokens == 0:
            raise HarnessError("token_bound_unverified")
        ceiling = {"generator": self.generation_reserve_tokens, "reviewer": self.review_reserve_tokens}
        if role in ceiling and bound.total_tokens > ceiling[role]:
            raise HarnessError("token_bound_unverified")
        return bound

    @staticmethod
    def _request_digest(role: str, payload: dict, bound: RequestBound) -> str:
        return canonical_digest({
            "role": role, "payload": payload, "bound_proof": bound.proof_digest,
            "reserved_tokens": bound.total_tokens, "output_tokens": bound.output_tokens,
        })

    async def replay(self, role: str, payload: dict, *,
                     output_limit_field: str | None = None) -> dict | None:
        bound = self._bound(role, payload, output_limit_field)
        self.authorize()
        return self.ledger.committed_result(
            self.owner, self.run_id, self.token, self._request_digest(role, payload, bound),
        )

    async def request(self, role: str, payload: dict, *,
                      output_limit_field: str | None = None) -> dict:
        payload = deepcopy(payload)
        bound = self._bound(role, payload, output_limit_field)
        self.authorize()
        timeout = self.remaining_seconds()
        if timeout <= 0:
            raise HarnessError("budget_exhausted")
        request = self.ledger.reserve(
            self.owner, self.run_id, self.token, role=role,
            endpoint_digest=self.endpoint_digest,
            request_digest=self._request_digest(role, payload, bound),
            reserved_tokens=bound.total_tokens,
            review_reserve_tokens=self.review_reserve_tokens,
            generation_reserve_tokens=self.generation_reserve_tokens,
        )
        identity = (self.owner, self.run_id, self.token,
                    request["request_id"], request["attempt_id"])
        try:
            self.authorize()
            timeout = min(timeout, self.remaining_seconds())
            if timeout <= 0:
                raise HarnessError("budget_exhausted")
            self.ledger.dispatch(*identity)
        except (Exception, asyncio.CancelledError):
            self.ledger.reject_unsent(*identity)
            raise
        try:
            # dispatched 是本地发送线性化点；此后的请求属于在途，不保证远端恰好一次。
            self.authorize()
            async with asyncio.timeout(timeout):
                response = await self.client.attempt(role, deepcopy(payload), timeout)
        except asyncio.CancelledError:
            self.ledger.mark_unknown(*identity)
            raise
        except Exception as exc:
            self.ledger.mark_unknown(*identity)
            if isinstance(exc, HarnessError) and exc.code == "lease_lost":
                raise
            raise HarnessError("completion_unknown") from exc
        actual = reported_tokens(response)
        self.ledger.settle(*identity, actual_tokens=actual, result=response)
        if actual is None:
            raise HarnessError("usage_unknown")
        if actual > bound.total_tokens:
            raise HarnessError("usage_exceeded")
        return response

    async def close(self):
        await self.client.close()
