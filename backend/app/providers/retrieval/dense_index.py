from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

_DENSE_INDEX_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


class DenseIndexStore:
    def __init__(
        self,
        *,
        manifest_path: Path,
        records_path: Path,
        vectors_path: Path,
        expected_model: str | None = None,
        expected_dimensions: int | None = None,
    ) -> None:
        self.manifest_path = manifest_path
        self.records_path = records_path
        self.vectors_path = vectors_path
        cache_key = self._build_cache_key(
            manifest_path=manifest_path,
            records_path=records_path,
            vectors_path=vectors_path,
            expected_model=expected_model,
            expected_dimensions=expected_dimensions,
        )
        cached_payload = _DENSE_INDEX_CACHE.get(cache_key)
        if cached_payload is not None:
            self._apply_cached_payload(cached_payload)
            return

        self.manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        self.records = self._load_jsonl(self.records_path)
        self.vectors = np.load(self.vectors_path, mmap_mode="r")

        if self.vectors.ndim != 2:
            raise ValueError("dense_vectors.f16.npy 不是二维矩阵。")
        if len(self.records) != int(self.vectors.shape[0]):
            raise ValueError("稠密索引记录数与向量行数不一致。")
        if expected_model and self.manifest.get("embedding_model") != expected_model:
            raise ValueError("稠密索引模型版本与当前配置不一致。")
        if expected_dimensions and int(self.vectors.shape[1]) != expected_dimensions:
            raise ValueError("稠密索引向量维度与当前配置不一致。")

        self.chunk_indices = np.asarray(
            [index for index, record in enumerate(self.records) if record.get("record_type") == "chunk"],
            dtype=np.int32,
        )
        self.rule_indices = np.asarray(
            [index for index, record in enumerate(self.records) if record.get("record_type") == "rule"],
            dtype=np.int32,
        )
        _DENSE_INDEX_CACHE[cache_key] = self._build_cached_payload()

    @property
    def version(self) -> str:
        return str(
            self.manifest.get("generated_at")
            or self.manifest.get("build_id")
            or self.manifest.get("source_hash")
            or ""
        )

    def search(
        self,
        *,
        query_vector: np.ndarray,
        top_k_chunks: int,
        top_k_rules: int,
    ) -> list[dict[str, Any]]:
        normalized_query = np.asarray(query_vector, dtype=np.float32)
        query_norm = float(np.linalg.norm(normalized_query))
        if query_norm == 0.0:
            raise ValueError("查询向量为空。")
        normalized_query = normalized_query / query_norm
        scores = np.asarray(self.vectors @ normalized_query, dtype=np.float32)
        candidates: list[dict[str, Any]] = []
        candidates.extend(self._collect_partition(scores, self.chunk_indices, top_k_chunks, "chunk"))
        candidates.extend(self._collect_partition(scores, self.rule_indices, top_k_rules, "rule"))
        candidates.sort(key=lambda item: item["dense_score"], reverse=True)
        return candidates

    def _collect_partition(
        self,
        scores: np.ndarray,
        indices: np.ndarray,
        top_k: int,
        record_type: str,
    ) -> list[dict[str, Any]]:
        if top_k <= 0 or indices.size == 0:
            return []
        top_n = min(top_k, int(indices.size))
        partition_scores = scores[indices]
        if top_n == int(indices.size):
            selected = np.argsort(partition_scores)[::-1]
        else:
            selected = np.argpartition(partition_scores, -top_n)[-top_n:]
            selected = selected[np.argsort(partition_scores[selected])[::-1]]

        results: list[dict[str, Any]] = []
        for local_rank, local_index in enumerate(selected, start=1):
            record_index = int(indices[int(local_index)])
            record = dict(self.records[record_index])
            dense_score = float(partition_scores[int(local_index)])
            record["record_type"] = record_type
            record["dense_rank"] = local_rank
            record["dense_score"] = dense_score
            record["score"] = dense_score
            record["retrieval_channels"] = ["dense"]
            results.append(record)
        return results

    @staticmethod
    def _load_jsonl(path: Path) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                payload = json.loads(stripped)
                if not isinstance(payload, dict):
                    raise ValueError("dense_records.jsonl 中存在非对象行。")
                records.append(payload)
        return records

    def _build_cached_payload(self) -> dict[str, Any]:
        return {
            "manifest": self.manifest,
            "records": self.records,
            "vectors": self.vectors,
            "chunk_indices": self.chunk_indices,
            "rule_indices": self.rule_indices,
        }

    def _apply_cached_payload(self, cached_payload: dict[str, Any]) -> None:
        self.manifest = cached_payload["manifest"]
        self.records = cached_payload["records"]
        self.vectors = cached_payload["vectors"]
        self.chunk_indices = cached_payload["chunk_indices"]
        self.rule_indices = cached_payload["rule_indices"]

    @classmethod
    def _build_cache_key(
        cls,
        *,
        manifest_path: Path,
        records_path: Path,
        vectors_path: Path,
        expected_model: str | None,
        expected_dimensions: int | None,
    ) -> tuple[Any, ...]:
        return (
            str(manifest_path.resolve()),
            cls._get_mtime_ns(manifest_path),
            str(records_path.resolve()),
            cls._get_mtime_ns(records_path),
            str(vectors_path.resolve()),
            cls._get_mtime_ns(vectors_path),
            expected_model or "",
            int(expected_dimensions) if expected_dimensions is not None else None,
        )

    @staticmethod
    def _get_mtime_ns(path: Path) -> int:
        return int(path.stat().st_mtime_ns)
