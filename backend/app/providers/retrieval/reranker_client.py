from __future__ import annotations

from typing import Any

import httpx


class RerankerClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.base_url = base_url.strip()
        self.model = model.strip()
        self.api_key = (api_key or "").strip()
        self.timeout_seconds = timeout_seconds

    def rerank(
        self,
        *,
        query: str,
        candidates: list[dict[str, Any]],
        top_n: int,
    ) -> list[dict[str, Any]]:
        if not candidates:
            return []

        response = httpx.post(
            self._build_rerank_url(self.base_url),
            headers=self._build_headers(),
            json={
                "model": self.model,
                "query": query,
                "texts": [self._build_text(candidate) for candidate in candidates],
                "top_n": min(top_n, len(candidates)),
                "truncate": True,
                "return_text": False,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        raw_results = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(raw_results, list):
            raise ValueError("重排服务返回结构缺少 results。")

        reranked: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                raise ValueError("重排服务返回结构错误。")
            index = item.get("index")
            if not isinstance(index, int) or index < 0 or index >= len(candidates):
                raise ValueError("重排服务返回的索引无效。")
            score = float(item.get("relevance_score", item.get("score", 0.0)))
            candidate = dict(candidates[index])
            candidate["rerank_score"] = score
            candidate["score"] = score
            reranked.append(candidate)
        return reranked

    def probe(self) -> dict[str, Any]:
        response = httpx.get(
            self._build_health_url(self.base_url),
            headers=self._build_headers(),
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return {
            "ok": True,
            "model": self.model,
            "endpoint": self._build_rerank_url(self.base_url),
        }

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _build_text(candidate: dict[str, Any]) -> str:
        title = str(candidate.get("title") or "").strip()
        content = str(candidate.get("content") or "").strip()
        if title and content:
            return f"{title}\n{content}"
        return title or content

    @staticmethod
    def _build_rerank_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if normalized.endswith("/rerank"):
            return normalized
        return f"{normalized}/rerank"

    @staticmethod
    def _build_health_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if normalized.endswith("/health"):
            return normalized
        return f"{normalized}/health"
