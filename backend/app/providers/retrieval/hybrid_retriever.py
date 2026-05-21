from __future__ import annotations

from typing import Any

import numpy as np

from app.providers.retrieval.base import BaseRetriever
from app.providers.retrieval.dense_index import DenseIndexStore
from app.providers.retrieval.embedding_client import EmbeddingClient
from app.providers.retrieval.reranker_client import RerankerClient

RRF_K = 60.0


class HybridRetriever(BaseRetriever):
    def __init__(
        self,
        *,
        sparse_retriever: BaseRetriever,
        embedding_client: EmbeddingClient | None,
        reranker_client: RerankerClient | None,
        dense_index: DenseIndexStore | None,
        dense_index_version: str | None,
        embedding_model: str | None,
        reranker_model: str | None,
        initial_config: dict[str, int],
        agentic_config: dict[str, int],
        fallback_reason: str | None = None,
    ) -> None:
        self.sparse_retriever = sparse_retriever
        self.embedding_client = embedding_client
        self.reranker_client = reranker_client
        self.dense_index = dense_index
        self.dense_index_version = dense_index_version or ""
        self.embedding_model = embedding_model or ""
        self.reranker_model = reranker_model or ""
        self.initial_config = initial_config
        self.agentic_config = agentic_config
        self.fallback_reason = (fallback_reason or "").strip()
        self.metadata: dict[str, Any] = {
            "provider": "hybrid_local",
            "mode": "hybrid" if self._hybrid_available else "sparse_only_fallback",
            "retrieval_degraded": not self._hybrid_available,
            "dense_index_version": self.dense_index_version,
            "embedding_model": self.embedding_model,
            "reranker_model": self.reranker_model,
        }

    @property
    def supports_query_search(self) -> bool:
        return self.sparse_retriever.supports_query_search

    @property
    def _hybrid_available(self) -> bool:
        return (
            self.embedding_client is not None
            and self.reranker_client is not None
            and self.dense_index is not None
            and not self.fallback_reason
        )

    def retrieve(self, accident_data: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
        query = self._build_initial_query(accident_data)
        limit = min(max(int(top_k), 1), self.initial_config["final_context_top_k"])
        if not self._hybrid_available:
            return self._fallback_retrieve(accident_data, query, limit, reason=self.fallback_reason or "混合检索依赖未就绪。")

        try:
            return self._run_hybrid(
                query=query,
                final_limit=limit,
                config=self.initial_config,
                enforce_type_balance=True,
                retrieval_mode="initial",
            )
        except Exception as exc:  # noqa: BLE001
            return self._fallback_retrieve(accident_data, query, limit, reason=str(exc))

    def search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        normalized_query = self._normalize_query(query)
        if not normalized_query:
            self.metadata = self._build_metadata(
                mode="sparse_only_fallback",
                query="",
                sparse_candidates=[],
                dense_candidates=[],
                merged_candidates=[],
                reranked_candidates=[],
                final_candidates=[],
                fallback_reason="补充检索查询为空。",
                retrieval_mode="agentic",
            )
            return []

        limit = max(int(top_k), 1)
        if not self._hybrid_available:
            return self._fallback_search(normalized_query, limit, reason=self.fallback_reason or "混合检索依赖未就绪。")

        try:
            return self._run_hybrid(
                query=normalized_query,
                final_limit=limit,
                config=self.agentic_config,
                enforce_type_balance=False,
                retrieval_mode="agentic",
            )
        except Exception as exc:  # noqa: BLE001
            return self._fallback_search(normalized_query, limit, reason=str(exc))

    def _run_hybrid(
        self,
        *,
        query: str,
        final_limit: int,
        config: dict[str, int],
        enforce_type_balance: bool,
        retrieval_mode: str,
    ) -> list[dict[str, Any]]:
        sparse_candidates = self._collect_sparse_candidates(
            query=query,
            top_k_chunks=config["sparse_top_k_chunks"],
            top_k_rules=config["sparse_top_k_rules"],
            merge_top_k=config["rrf_merge_top_k"],
        )
        query_vector = np.asarray(self.embedding_client.embed_query(query), dtype=np.float32)
        dense_candidates = self.dense_index.search(
            query_vector=query_vector,
            top_k_chunks=config["dense_top_k_chunks"],
            top_k_rules=config["dense_top_k_rules"],
        )
        merged_candidates = self._merge_candidates(
            sparse_candidates=sparse_candidates,
            dense_candidates=dense_candidates,
            merge_top_k=config["rrf_merge_top_k"],
        )
        reranked_candidates = self.reranker_client.rerank(
            query=query,
            candidates=merged_candidates,
            top_n=min(config["rerank_top_k"], len(merged_candidates)),
        )
        final_candidates = self._select_final_candidates(
            candidates=reranked_candidates,
            limit=final_limit,
            max_context_chars=config["max_context_chars"],
            enforce_type_balance=enforce_type_balance,
        )
        self.metadata = self._build_metadata(
            mode="hybrid",
            query=query,
            sparse_candidates=sparse_candidates,
            dense_candidates=dense_candidates,
            merged_candidates=merged_candidates,
            reranked_candidates=reranked_candidates,
            final_candidates=final_candidates,
            fallback_reason=None,
            retrieval_mode=retrieval_mode,
        )
        return final_candidates

    def _collect_sparse_candidates(
        self,
        *,
        query: str,
        top_k_chunks: int,
        top_k_rules: int,
        merge_top_k: int,
    ) -> list[dict[str, Any]]:
        requested_top_k = max(top_k_chunks + top_k_rules + 6, merge_top_k)
        if not self.sparse_retriever.supports_query_search:
            return []
        raw_candidates = self.sparse_retriever.search(query=query, top_k=requested_top_k)
        chunks: list[dict[str, Any]] = []
        rules: list[dict[str, Any]] = []
        for item in raw_candidates:
            record_type = str(item.get("record_type") or "").strip().lower()
            if record_type == "chunk" and len(chunks) < top_k_chunks:
                chunks.append(dict(item))
            elif record_type == "rule" and len(rules) < top_k_rules:
                rules.append(dict(item))
            if len(chunks) >= top_k_chunks and len(rules) >= top_k_rules:
                break

        candidates = chunks + rules
        candidates.sort(key=lambda item: float(item.get("score", 0.0)), reverse=True)
        for rank, item in enumerate(candidates, start=1):
            item["sparse_rank"] = rank
            item["sparse_score"] = float(item.get("score", 0.0))
            item["retrieval_channels"] = ["sparse"]
        return candidates

    def _merge_candidates(
        self,
        *,
        sparse_candidates: list[dict[str, Any]],
        dense_candidates: list[dict[str, Any]],
        merge_top_k: int,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for rank, item in enumerate(sparse_candidates, start=1):
            key = self._candidate_key(item)
            merged_item = merged.setdefault(key, dict(item))
            merged_item["rrf_score"] = merged_item.get("rrf_score", 0.0) + (1.0 / (RRF_K + rank))
            merged_item["retrieval_channels"] = self._merge_channels(
                merged_item.get("retrieval_channels"),
                ["sparse"],
            )

        for rank, item in enumerate(dense_candidates, start=1):
            key = self._candidate_key(item)
            merged_item = merged.setdefault(key, dict(item))
            merged_item["rrf_score"] = merged_item.get("rrf_score", 0.0) + (1.0 / (RRF_K + rank))
            merged_item["retrieval_channels"] = self._merge_channels(
                merged_item.get("retrieval_channels"),
                ["dense"],
            )
            if "dense_score" in item:
                merged_item["dense_score"] = item["dense_score"]
            if "dense_rank" in item:
                merged_item["dense_rank"] = item["dense_rank"]

        merged_list = list(merged.values())
        merged_list.sort(key=lambda item: float(item.get("rrf_score", 0.0)), reverse=True)
        return merged_list[:merge_top_k]

    def _select_final_candidates(
        self,
        *,
        candidates: list[dict[str, Any]],
        limit: int,
        max_context_chars: int,
        enforce_type_balance: bool,
    ) -> list[dict[str, Any]]:
        if limit <= 0:
            return []

        selected: list[dict[str, Any]] = []
        remaining = list(candidates)
        type_cap = max(limit - 1, 1)
        type_counts = {"chunk": 0, "rule": 0}

        if enforce_type_balance:
            for record_type in ("chunk", "rule"):
                candidate = next(
                    (item for item in remaining if str(item.get("record_type") or "") == record_type),
                    None,
                )
                if candidate is not None and len(selected) < limit:
                    selected.append(dict(candidate))
                    type_counts[record_type] += 1
                    remaining = [item for item in remaining if self._candidate_key(item) != self._candidate_key(candidate)]

        for item in remaining:
            record_type = str(item.get("record_type") or "").strip()
            if enforce_type_balance and record_type in type_counts and type_counts[record_type] >= type_cap:
                continue
            selected.append(dict(item))
            if record_type in type_counts:
                type_counts[record_type] += 1
            if len(selected) >= limit:
                break

        return self._truncate_by_budget(selected, max_context_chars)

    def _truncate_by_budget(
        self,
        candidates: list[dict[str, Any]],
        max_context_chars: int,
    ) -> list[dict[str, Any]]:
        remaining_budget = max_context_chars
        truncated: list[dict[str, Any]] = []
        for item in candidates:
            content = str(item.get("content") or "")
            if not content:
                truncated.append(item)
                continue
            if remaining_budget <= 0:
                break
            reserved = 120
            allowed = min(len(content), max(160, remaining_budget - reserved))
            if allowed < len(content):
                item["content"] = f"{content[:allowed].rstrip()}..."
                item["content_truncated"] = True
            remaining_budget -= len(str(item.get("content") or ""))
            truncated.append(item)
        return truncated

    def _fallback_retrieve(
        self,
        accident_data: dict[str, Any],
        query: str,
        limit: int,
        *,
        reason: str,
    ) -> list[dict[str, Any]]:
        results = self.sparse_retriever.retrieve(accident_data=accident_data, top_k=limit)
        sparse_metadata = dict(getattr(self.sparse_retriever, "metadata", {}))
        self.metadata = self._build_metadata(
            mode="sparse_only_fallback",
            query=query,
            sparse_candidates=results,
            dense_candidates=[],
            merged_candidates=results,
            reranked_candidates=results,
            final_candidates=results,
            fallback_reason=reason,
            sparse_metadata=sparse_metadata,
            retrieval_mode="initial",
        )
        return results

    def _fallback_search(self, query: str, limit: int, *, reason: str) -> list[dict[str, Any]]:
        if not self.sparse_retriever.supports_query_search:
            self.metadata = self._build_metadata(
                mode="sparse_only_fallback",
                query=query,
                sparse_candidates=[],
                dense_candidates=[],
                merged_candidates=[],
                reranked_candidates=[],
                final_candidates=[],
                fallback_reason=reason,
                retrieval_mode="agentic",
            )
            return []

        results = self.sparse_retriever.search(query=query, top_k=limit)
        sparse_metadata = dict(getattr(self.sparse_retriever, "metadata", {}))
        self.metadata = self._build_metadata(
            mode="sparse_only_fallback",
            query=query,
            sparse_candidates=results,
            dense_candidates=[],
            merged_candidates=results,
            reranked_candidates=results,
            final_candidates=results,
            fallback_reason=reason,
            sparse_metadata=sparse_metadata,
            retrieval_mode="agentic",
        )
        return results

    def _build_metadata(
        self,
        *,
        mode: str,
        query: str,
        sparse_candidates: list[dict[str, Any]],
        dense_candidates: list[dict[str, Any]],
        merged_candidates: list[dict[str, Any]],
        reranked_candidates: list[dict[str, Any]],
        final_candidates: list[dict[str, Any]],
        fallback_reason: str | None,
        sparse_metadata: dict[str, Any] | None = None,
        retrieval_mode: str | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "provider": "hybrid_local",
            "mode": mode,
            "retrieval_degraded": mode != "hybrid",
            "fallback_reason": fallback_reason,
            "embedding_model": self.embedding_model,
            "reranker_model": self.reranker_model,
            "dense_index_version": self.dense_index_version,
            "initial_query": query,
            "last_query": query,
            "sparse_candidate_count": len(sparse_candidates),
            "dense_candidate_count": len(dense_candidates),
            "merged_candidate_count": len(merged_candidates),
            "reranked_count": len(reranked_candidates),
            "final_candidate_count": len(final_candidates),
        }
        if retrieval_mode:
            metadata["retrieval_mode"] = retrieval_mode
        if sparse_metadata:
            for key in ("catalog_version", "search_index_loaded"):
                if key in sparse_metadata:
                    metadata[key] = sparse_metadata[key]
        return metadata

    def _build_initial_query(self, accident_data: dict[str, Any]) -> str:
        preferred_keys = [
            "事故标题",
            "事故类型与形态",
            "事故认定原因",
            "事故经过",
            "事故地点",
            "天气情况",
            "道路情况",
            "当事人行为",
        ]
        values: list[str] = []
        seen: set[str] = set()
        for key in preferred_keys:
            raw_value = accident_data.get(key)
            if isinstance(raw_value, str):
                normalized = self._normalize_query(raw_value)
                if normalized and normalized not in seen:
                    values.append(normalized)
                    seen.add(normalized)

        if not values:
            for value in accident_data.values():
                if isinstance(value, str):
                    normalized = self._normalize_query(value)
                    if normalized and normalized not in seen:
                        values.append(normalized)
                        seen.add(normalized)

        query = " ".join(values)
        if len(query) > 280:
            query = query[:280]
        return query

    @staticmethod
    def _normalize_query(text: str) -> str:
        return " ".join(str(text).split()).strip()

    @staticmethod
    def _candidate_key(candidate: dict[str, Any]) -> str:
        key = str(candidate.get("id") or "").strip()
        if key:
            return key
        return f"{candidate.get('record_type', '')}:{candidate.get('title', '')}"

    @staticmethod
    def _merge_channels(existing: Any, new_channels: list[str]) -> list[str]:
        merged = []
        for channel in list(existing or []) + list(new_channels):
            if channel not in merged:
                merged.append(channel)
        return merged
