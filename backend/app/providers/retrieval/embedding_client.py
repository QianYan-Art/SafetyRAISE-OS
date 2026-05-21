from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import Any

import httpx

from app.providers.llm.lmstudio_compat import resolve_lmstudio_compatibility
from app.providers.lmstudio_residency import LMStudioResidencyManager, LMStudioResidencySpec


class EmbeddingClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        provider_name: str | None = None,
        timeout_seconds: int = 60,
        lmstudio_host_allowlist: list[str] | None = None,
        lmstudio_ttl_seconds: int | None = None,
        lmstudio_residency_manager: LMStudioResidencyManager | None = None,
        residency_companions: list[LMStudioResidencySpec] | None = None,
        query_instruction: str = "",
        query_max_length: int = 512,
        document_max_length: int = 2048,
        cache_size: int = 512,
        cache_ttl_seconds: int = 21600,
    ) -> None:
        self.base_url = base_url.strip()
        self.model = model.strip()
        self.api_key = (api_key or "").strip()
        self.provider_name = (provider_name or "").strip()
        self.timeout_seconds = timeout_seconds
        self._lmstudio_residency_manager = lmstudio_residency_manager
        self._lmstudio_residency_companions = list(residency_companions or [])
        self._lmstudio = resolve_lmstudio_compatibility(
            base_url_or_endpoint=self.base_url,
            provider_name=self.provider_name,
            host_allowlist=lmstudio_host_allowlist,
            ttl_seconds=lmstudio_ttl_seconds,
        )
        self.query_instruction = query_instruction.strip()
        self.query_max_length = query_max_length
        self.document_max_length = document_max_length
        self.cache_size = cache_size
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
        self._cache_lock = Lock()

    def embed_query(self, query: str) -> list[float]:
        normalized_query = self._truncate_text(query, self.query_max_length)
        if not normalized_query:
            raise ValueError("检索查询不能为空。")

        cached = self._get_cached(normalized_query)
        if cached is not None:
            return cached

        payload = self._request_embeddings([self._format_query(normalized_query)])[0]
        self._set_cached(normalized_query, payload)
        return payload

    def probe(self) -> dict[str, Any]:
        vector = self._request_embeddings([self._format_query("交通事故责任认定")])[0]
        return {
            "ok": True,
            "model": self.model,
            "dimensions": len(vector),
            "endpoint": self._build_embeddings_url(self.base_url),
        }

    def _request_embeddings(self, texts: list[str]) -> list[list[float]]:
        self._ensure_lmstudio_residency()
        payload = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
        }
        if self._lmstudio.enabled and self._lmstudio.ttl_seconds is not None:
            payload["ttl"] = self._lmstudio.ttl_seconds
        response = httpx.post(
            self._build_embeddings_url(self.base_url),
            headers=self._build_headers(),
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        items = payload.get("data")
        if not isinstance(items, list) or not items:
            raise ValueError("嵌入服务返回结构缺少 data。")

        vectors: list[list[float]] = []
        for item in items:
            embedding = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(embedding, list) or not embedding:
                raise ValueError("嵌入服务返回结构缺少 embedding。")
            vectors.append([float(value) for value in embedding])
        return vectors

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _format_query(self, query: str) -> str:
        if not self.query_instruction:
            return query
        return f"Instruct: {self.query_instruction}\nQuery: {query}"

    def _ensure_lmstudio_residency(self) -> None:
        if self._lmstudio_residency_manager is None or not self._lmstudio.enabled:
            return
        self._lmstudio_residency_manager.ensure_residency(
            primary_spec=LMStudioResidencySpec(
                model=self.model,
                base_url_or_endpoint=self.base_url,
                api_key=self.api_key,
                provider_name=self.provider_name,
                ttl_seconds=self._lmstudio.ttl_seconds,
            ),
            companion_specs=self._lmstudio_residency_companions,
        )

    def _get_cached(self, query: str) -> list[float] | None:
        now = time.time()
        with self._cache_lock:
            item = self._cache.get(query)
            if item is None:
                return None
            created_at, vector = item
            if now - created_at > self.cache_ttl_seconds:
                self._cache.pop(query, None)
                return None
            self._cache.move_to_end(query)
            return list(vector)

    def _set_cached(self, query: str, vector: list[float]) -> None:
        with self._cache_lock:
            self._cache[query] = (time.time(), list(vector))
            self._cache.move_to_end(query)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        normalized = " ".join(str(text).split())
        return normalized[:limit]

    @staticmethod
    def _build_embeddings_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        if normalized.endswith("/embeddings"):
            return normalized
        if normalized.endswith("/v1"):
            return f"{normalized}/embeddings"
        return f"{normalized}/v1/embeddings"
