from collections import Counter, defaultdict
import ctypes
import json
import math
import os
import re
from pathlib import Path
from typing import Any

from app.core.exceptions import ProviderError
from app.core.rust_accel import load_rust_token_accel
from app.providers.retrieval.base import BaseRetriever

_LOCAL_JSONL_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


class LocalJsonlRetriever(BaseRetriever):
    def __init__(
        self,
        manifest_path: Path | None = None,
        chunks_path: Path | None = None,
        rules_path: Path | None = None,
        search_index_path: Path | None = None,
        min_score: float = 0.2,
        top_k_chunks: int = 3,
        top_k_rules: int = 2,
        max_context_chars: int = 4000,
        prefer_enhanced_rules: bool = True,
        enable_search_index: bool = True,
        watch_manifest_changes: bool = True,
    ):
        self.min_score = min_score
        self.top_k_chunks = top_k_chunks
        self.top_k_rules = top_k_rules
        self.max_context_chars = max_context_chars
        self.prefer_enhanced_rules = prefer_enhanced_rules
        self.enable_search_index = enable_search_index
        self.watch_manifest_changes = watch_manifest_changes

        self.data_dir = self._resolve_data_dir(
            manifest_path=manifest_path,
            chunks_path=chunks_path,
            rules_path=rules_path,
            search_index_path=search_index_path,
        )
        self.manifest_path = self._resolve_manifest_path(manifest_path)
        self.chunks_path = self._resolve_chunks_path(chunks_path)
        self.rules_path = self._resolve_rules_path(rules_path)
        self.search_index_path = self._resolve_search_index_path(search_index_path)

        self._manifest: dict[str, Any] | None = None
        self._chunk_records: list[dict[str, Any]] | None = None
        self._rule_records: list[dict[str, Any]] | None = None
        self._chunk_by_id: dict[str, dict[str, Any]] = {}
        self._rule_by_id: dict[str, dict[str, Any]] = {}
        self._search_index: dict[str, Any] | None = None
        self._topic_index: dict[str, Any] = {}
        self._relation_index: dict[str, Any] = {}
        self._synonym_map: dict[str, set[str]] = {}
        self._chunk_inverted_index: dict[str, list[dict[str, Any]]] = {}
        self._rule_inverted_index: dict[str, list[dict[str, Any]]] = {}
        self._topic_entries: list[dict[str, Any]] = []
        self._case_to_rules: dict[str, list[str]] = {}
        self._chunk_rust_payload_by_id: dict[str, dict[str, Any]] = {}
        self._rule_rust_payload_by_id: dict[str, dict[str, Any]] = {}
        self._record_score_cache: dict[str, dict[str, Any]] = {}
        self._manifest_mtime_ns: int | None = None
        self.metadata: dict[str, Any] = {
            "provider": "local_jsonl",
            "retrieval_degraded": False,
            "data_dir": str(self.data_dir),
            "search_index_enabled": self.enable_search_index,
            "watch_manifest_changes": self.watch_manifest_changes,
            "token_accel": "rust" if load_rust_token_accel() is not None else "python",
        }
        self._ensure_loaded(force=True)

    @property
    def supports_query_search(self) -> bool:
        return True

    def retrieve(self, accident_data: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
        query = self._build_initial_query(accident_data)
        if not query:
            return []

        results = self.search(query=query, top_k=top_k)
        self.metadata["initial_query"] = query
        return results

    def search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        self._ensure_loaded()

        normalized_query = re.sub(r"\s+", " ", (query or "")).strip()
        if not normalized_query:
            return []

        query_tokens = self._build_query_tokens(normalized_query)
        if not query_tokens:
            return []

        self.metadata["last_query"] = normalized_query
        self.metadata["last_query_tokens"] = query_tokens[:20]

        if self._search_index:
            ranked = self._search_with_index(normalized_query, query_tokens, top_k)
            strategy = "search_index"
        else:
            ranked = self._search_by_scan(query_tokens, top_k)
            strategy = "full_scan"

        ranked = self._deduplicate(ranked)[:top_k]
        ranked = self._truncate_context(ranked)
        self.metadata["last_strategy"] = strategy
        self.metadata["last_results_count"] = len(ranked)
        return ranked

    def _ensure_loaded(self, force: bool = False) -> None:
        should_reload = force or self._chunk_records is None or self._rule_records is None
        if not should_reload and self.watch_manifest_changes:
            should_reload = self._has_manifest_changed()

        if not should_reload:
            return

        cache_key = self._build_cache_key()
        cached_payload = _LOCAL_JSONL_CACHE.get(cache_key)
        if cached_payload is not None:
            self._apply_cached_payload(cached_payload)
            return

        self._manifest = self._load_json(self.manifest_path)
        self._chunk_records = self._load_jsonl(self.chunks_path)
        self._rule_records = self._load_jsonl(self.rules_path)
        self._chunk_by_id = {
            str(item.get("chunk_id")): item for item in self._chunk_records if item.get("chunk_id")
        }
        self._rule_by_id = {
            str(item.get("rule_id")): item for item in self._rule_records if item.get("rule_id")
        }
        self._chunk_rust_payload_by_id = self._build_rust_payload_cache(self._chunk_by_id, "chunk")
        self._rule_rust_payload_by_id = self._build_rust_payload_cache(self._rule_by_id, "rule")
        self._record_score_cache = self._build_record_score_cache()
        self._load_search_index()
        self._manifest_mtime_ns = self._get_mtime_ns(self.manifest_path)

        catalog_meta = self._manifest.get("catalog_meta", {})
        self.metadata.update(self._build_loaded_metadata(catalog_meta))
        _LOCAL_JSONL_CACHE[cache_key] = self._build_cached_payload()

    def _build_loaded_metadata(self, catalog_meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "catalog_version": catalog_meta.get("catalog_version", ""),
            "generated_at": self._manifest.get("generated_at", ""),
            "manifest_path": str(self.manifest_path),
            "chunks_path": str(self.chunks_path),
            "rules_path": str(self.rules_path),
            "search_index_path": str(self.search_index_path),
            "chunk_records": len(self._chunk_records or []),
            "rule_records": len(self._rule_records or []),
            "rules_file_used": self.rules_path.name,
        }

    def _build_cached_payload(self) -> dict[str, Any]:
        return {
            "manifest": self._manifest,
            "chunk_records": self._chunk_records,
            "rule_records": self._rule_records,
            "chunk_by_id": self._chunk_by_id,
            "rule_by_id": self._rule_by_id,
            "search_index": self._search_index,
            "topic_index": self._topic_index,
            "relation_index": self._relation_index,
            "synonym_map": self._synonym_map,
            "chunk_inverted_index": self._chunk_inverted_index,
            "rule_inverted_index": self._rule_inverted_index,
            "topic_entries": self._topic_entries,
            "case_to_rules": self._case_to_rules,
            "chunk_rust_payload_by_id": self._chunk_rust_payload_by_id,
            "rule_rust_payload_by_id": self._rule_rust_payload_by_id,
            "record_score_cache": self._record_score_cache,
            "manifest_mtime_ns": self._manifest_mtime_ns,
            "metadata": {
                "search_index_loaded": self.metadata.get("search_index_loaded", False),
                "search_index_generated_at": self.metadata.get("search_index_generated_at", ""),
                "topic_count": self.metadata.get("topic_count", 0),
                "synonym_count": self.metadata.get("synonym_count", 0),
                "search_index_error": self.metadata.get("search_index_error"),
                **self._build_loaded_metadata(self._manifest.get("catalog_meta", {})),
            },
        }

    def _apply_cached_payload(self, cached_payload: dict[str, Any]) -> None:
        self._manifest = cached_payload["manifest"]
        self._chunk_records = cached_payload["chunk_records"]
        self._rule_records = cached_payload["rule_records"]
        self._chunk_by_id = cached_payload["chunk_by_id"]
        self._rule_by_id = cached_payload["rule_by_id"]
        self._search_index = cached_payload["search_index"]
        self._topic_index = cached_payload["topic_index"]
        self._relation_index = cached_payload["relation_index"]
        self._synonym_map = cached_payload["synonym_map"]
        self._chunk_inverted_index = cached_payload["chunk_inverted_index"]
        self._rule_inverted_index = cached_payload["rule_inverted_index"]
        self._topic_entries = cached_payload["topic_entries"]
        self._case_to_rules = cached_payload["case_to_rules"]
        self._chunk_rust_payload_by_id = cached_payload["chunk_rust_payload_by_id"]
        self._rule_rust_payload_by_id = cached_payload["rule_rust_payload_by_id"]
        self._record_score_cache = cached_payload["record_score_cache"]
        self._manifest_mtime_ns = cached_payload["manifest_mtime_ns"]

        search_index_error = cached_payload["metadata"].get("search_index_error")
        if search_index_error:
            self.metadata["search_index_error"] = search_index_error
        else:
            self.metadata.pop("search_index_error", None)
        self.metadata.update(cached_payload["metadata"])

    def _build_cache_key(self) -> tuple[Any, ...]:
        search_index_mtime_ns = None
        if self.enable_search_index and self.search_index_path.exists():
            search_index_mtime_ns = self._get_mtime_ns(self.search_index_path)
        return (
            str(self.manifest_path),
            self._get_mtime_ns(self.manifest_path),
            str(self.chunks_path),
            self._get_mtime_ns(self.chunks_path),
            str(self.rules_path),
            self._get_mtime_ns(self.rules_path),
            str(self.search_index_path),
            search_index_mtime_ns,
            self.enable_search_index,
        )

    def _load_json(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise ProviderError(f"本地知识库文件不存在: {path}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProviderError(f"解析知识库清单失败: {path}") from exc

    def _load_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            raise ProviderError(f"本地知识库文件不存在: {path}")

        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                content = line.strip()
                if not content:
                    continue
                try:
                    item = json.loads(content)
                except json.JSONDecodeError as exc:
                    raise ProviderError(f"解析知识库文件失败: {path}:{line_no}") from exc
                if isinstance(item, dict):
                    rows.append(item)
        return rows

    def _load_search_index(self) -> None:
        self._search_index = None
        self._topic_index = {}
        self._relation_index = {}
        self._synonym_map = {}
        self._chunk_inverted_index = {}
        self._rule_inverted_index = {}
        self._topic_entries = []
        self._case_to_rules = {}
        self.metadata.pop("search_index_error", None)

        if not self.enable_search_index:
            self.metadata["search_index_loaded"] = False
            return

        if not self.search_index_path.exists():
            self.metadata["search_index_loaded"] = False
            return

        try:
            payload = json.loads(self.search_index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            self.metadata["search_index_loaded"] = False
            self.metadata["search_index_error"] = f"{type(exc).__name__}: {exc}"
            return

        self._search_index = payload
        self._topic_index = payload.get("topic_index", {})
        self._relation_index = payload.get("relation_index", {})
        indexes = payload.get("indexes", {})
        self._chunk_inverted_index = indexes.get("chunk_inverted", {}) if isinstance(indexes, dict) else {}
        self._rule_inverted_index = indexes.get("rule_inverted", {}) if isinstance(indexes, dict) else {}
        self._synonym_map = self._build_synonym_map(payload.get("synonyms", {}))
        self._topic_entries = self._build_topic_entries(self._topic_index)
        case_to_rules = self._relation_index.get("case_to_rules", {})
        self._case_to_rules = {
            str(key): [str(rule_id) for rule_id in values or [] if str(rule_id).strip()]
            for key, values in case_to_rules.items()
        } if isinstance(case_to_rules, dict) else {}
        self.metadata["search_index_loaded"] = True
        self.metadata["search_index_generated_at"] = payload.get("generated_at", "")
        self.metadata["topic_count"] = len(self._topic_index)
        self.metadata["synonym_count"] = len(self._synonym_map)

    def _build_initial_query(self, accident_data: dict[str, Any]) -> str:
        preferred_keys = [
            "事故标题",
            "事故类型",
            "事故形态",
            "事故认定原因",
            "主要违法行为",
            "车辆类型",
            "路口路段类型",
            "地点（包括路名，路号）",
        ]
        text_parts: list[str] = []
        for key in preferred_keys:
            value = accident_data.get(key)
            if isinstance(value, str) and value.strip():
                text_parts.append(value.strip())

        if not text_parts:
            for value in accident_data.values():
                if isinstance(value, str) and value.strip():
                    text_parts.append(value.strip())

        deduped = list(dict.fromkeys(text_parts))
        return "\n".join(deduped[:8])

    def _build_query_tokens(self, text: str) -> list[str]:
        counter: Counter[str] = Counter(self._tokenize(text))
        for token in list(counter):
            for expanded in self._synonym_map.get(token, set()):
                counter[expanded] += 1

        ranked = sorted(counter.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
        return [token for token, _ in ranked[:30]]

    def _tokenize(self, text: str) -> set[str]:
        rust_accel = load_rust_token_accel()
        if rust_accel is not None:
            rust_tokens = self._tokenize_with_rust(rust_accel, text)
            if rust_tokens is not None:
                return rust_tokens

        normalized = re.sub(r"\s+", " ", text).strip().lower()
        segments = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9_.:/-]{2,}", normalized)
        tokens: set[str] = set()
        stop_words = {
            "需要关注",
            "信息不足",
            "待核实",
            "page",
            "latest",
            "报告",
            "指出",
            "导致",
            "发生",
            "需要",
            "核对",
            "分析",
            "请求",
            "检索",
            "知识库",
        }
        for segment in segments:
            if segment in stop_words:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", segment):
                tokens.add(segment)
                if len(segment) <= 4:
                    continue
                for size in (4, 3, 2):
                    for idx in range(len(segment) - size + 1):
                        token = segment[idx : idx + size]
                        if token in stop_words:
                            continue
                        tokens.add(token)
            elif not segment.isdigit():
                tokens.add(segment)
        return {item for item in tokens if len(item) >= 2}

    @staticmethod
    def _tokenize_with_rust(rust_accel, text: str) -> set[str] | None:  # noqa: ANN001
        try:
            raw_ptr = rust_accel.accel_tokenize_text(text.encode("utf-8"))
            if not raw_ptr:
                return None
            try:
                payload = ctypes.string_at(raw_ptr).decode("utf-8")
            finally:
                rust_accel.accel_free_string(raw_ptr)
        except Exception:  # noqa: BLE001
            return None

        tokens = {line.strip() for line in payload.splitlines() if line.strip()}
        return tokens or None

    def _search_with_index(
        self,
        query: str,
        query_tokens: list[str],
        top_k: int,
    ) -> list[dict[str, Any]]:
        chunk_states = self._collect_index_candidates(query_tokens, record_type="chunk")
        rule_states = self._collect_index_candidates(query_tokens, record_type="rule")

        self._boost_topic_rules(query=query, query_tokens=query_tokens, rule_states=rule_states)
        self._boost_related_rules(chunk_states=chunk_states, rule_states=rule_states)

        rust_accel = load_rust_token_accel()
        limit = max(top_k * 2, self.top_k_chunks + self.top_k_rules)
        if rust_accel is not None:
            ranked = self._score_with_rust(
                rust_accel=rust_accel,
                query_tokens=query_tokens,
                chunk_states=chunk_states,
                rule_states=rule_states,
                limit=limit,
            )
            if ranked is not None:
                return ranked

        ranked: list[dict[str, Any]] = []
        for record_id, state in chunk_states.items():
            record = self._chunk_by_id.get(record_id)
            if not record:
                continue
            score = self._compute_index_score(record=record, state=state, query_tokens=query_tokens)
            if score >= self.min_score:
                ranked.append(self._to_result(record=record, score=score, record_type="chunk"))

        for record_id, state in rule_states.items():
            record = self._rule_by_id.get(record_id)
            if not record:
                continue
            score = self._compute_index_score(record=record, state=state, query_tokens=query_tokens)
            if score >= self.min_score:
                ranked.append(self._to_result(record=record, score=score, record_type="rule"))

        ranked.sort(key=lambda item: item["score"], reverse=True)
        if ranked:
            return ranked[:limit]
        return self._search_by_scan(query_tokens, top_k)

    def _score_with_rust(
        self,
        *,
        rust_accel,  # noqa: ANN001
        query_tokens: list[str],
        chunk_states: dict[str, dict[str, Any]],
        rule_states: dict[str, dict[str, Any]],
        limit: int,
    ) -> list[dict[str, Any]] | None:
        candidates: list[dict[str, Any]] = []
        record_lookup: dict[tuple[str, str], dict[str, Any]] = {}

        for record_type, states, mapping in (
            ("chunk", chunk_states, self._chunk_by_id),
            ("rule", rule_states, self._rule_by_id),
        ):
            payload_cache = self._chunk_rust_payload_by_id if record_type == "chunk" else self._rule_rust_payload_by_id
            for record_id, state in states.items():
                record = mapping.get(record_id)
                if not record:
                    continue
                key = (record_type, record_id)
                record_lookup[key] = record
                payload = dict(payload_cache.get(record_id) or self._build_rust_payload(record, record_type))
                payload["state"] = {
                    "match_count": len(state["match_tokens"]),
                    "tf_sum": float(state["tf_sum"]),
                    "title_hits": int(state["title_hits"]),
                    "bonus": float(state["bonus"]),
                }
                candidates.append(payload)

        if not candidates:
            return []

        try:
            payload = json.dumps(
                {
                    "query_tokens": query_tokens,
                    "min_score": self.min_score,
                    "limit": limit,
                    "candidates": candidates,
                },
                ensure_ascii=False,
            )
            raw_ptr = rust_accel.accel_score_records(payload.encode("utf-8"))
            if not raw_ptr:
                return None
            try:
                raw_payload = ctypes.string_at(raw_ptr).decode("utf-8")
            finally:
                rust_accel.accel_free_string(raw_ptr)
            items = json.loads(raw_payload)
        except Exception:  # noqa: BLE001
            return None

        ranked: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            record_type = str(item.get("record_type", ""))
            record_id = str(item.get("id", ""))
            record = record_lookup.get((record_type, record_id))
            if not record:
                continue
            try:
                score = float(item.get("score", 0.0))
            except (TypeError, ValueError):
                continue
            ranked.append(self._to_result(record=record, score=score, record_type=record_type))
        return ranked

    def _collect_index_candidates(
        self,
        query_tokens: list[str],
        record_type: str,
    ) -> dict[str, dict[str, Any]]:
        if not self._search_index:
            return {}

        inverted = self._chunk_inverted_index if record_type == "chunk" else self._rule_inverted_index
        states: dict[str, dict[str, Any]] = {}

        for token in query_tokens:
            for item in inverted.get(token, []):
                record_id = str(item.get("id", ""))
                if not record_id:
                    continue
                state = states.setdefault(record_id, self._new_score_state())
                state["match_tokens"].add(token)
                state["tf_sum"] += math.log1p(float(item.get("tf", 1) or 1))
                title = str(item.get("title", "")).lower()
                if token in title:
                    state["title_hits"] += 1

        return states

    def _boost_topic_rules(
        self,
        query: str,
        query_tokens: list[str],
        rule_states: dict[str, dict[str, Any]],
    ) -> None:
        lowered_query = query.lower()
        query_token_set = set(query_tokens)
        for topic_entry in self._topic_entries:
            topic_hit = topic_entry["topic_name"] in lowered_query or any(
                keyword in lowered_query or keyword in query_token_set for keyword in topic_entry["keywords"]
            )
            if not topic_hit:
                continue

            for index, rule_id in enumerate(topic_entry["rule_ids"][: self.top_k_rules * 4]):
                if not rule_id:
                    continue
                state = rule_states.setdefault(rule_id, self._new_score_state())
                state["bonus"] += max(0.18 - (index * 0.02), 0.04)

    def _boost_related_rules(
        self,
        chunk_states: dict[str, dict[str, Any]],
        rule_states: dict[str, dict[str, Any]],
    ) -> None:
        ranked_cases = sorted(
            chunk_states.items(),
            key=lambda item: (
                -len(item[1]["match_tokens"]),
                -item[1]["tf_sum"],
                item[0],
            ),
        )

        for _, (chunk_id, _) in enumerate(ranked_cases[:3]):
            related_rule_ids = self._case_to_rules.get(chunk_id, [])
            for index, rule_id in enumerate(related_rule_ids[:5]):
                state = rule_states.setdefault(str(rule_id), self._new_score_state())
                state["bonus"] += max(0.10 - (index * 0.01), 0.03)

    def _compute_index_score(
        self,
        record: dict[str, Any],
        state: dict[str, Any],
        query_tokens: list[str],
    ) -> float:
        token_count = max(min(len(query_tokens), 8), 1)
        match_count = len(state["match_tokens"])
        coverage_score = (match_count / token_count) * 0.45
        tf_score = min(state["tf_sum"] / 6, 0.22)
        title_score = min(state["title_hits"], 3) * 0.06
        field_score = 0.05 if record.get("category") or record.get("rule_type") else 0.0
        semantic_score = min(self._score_record(record, query_tokens), 0.35)
        bonus_score = min(float(state["bonus"]), 0.18)
        return round(min(coverage_score + tf_score + title_score + field_score + semantic_score + bonus_score, 1.0), 4)

    def _search_by_scan(self, query_tokens: list[str], top_k: int) -> list[dict[str, Any]]:
        chunk_limit = max(top_k, self.top_k_chunks)
        rule_limit = max(top_k, self.top_k_rules)
        chunk_hits = self._search_records(self._chunk_records or [], query_tokens, chunk_limit, "chunk")
        rule_hits = self._search_records(self._rule_records or [], query_tokens, rule_limit, "rule")
        ranked = sorted(chunk_hits + rule_hits, key=lambda item: item["score"], reverse=True)
        return ranked[: max(top_k * 2, self.top_k_chunks + self.top_k_rules)]

    def _search_records(
        self,
        records: list[dict[str, Any]],
        query_tokens: list[str],
        limit: int,
        record_type: str,
    ) -> list[dict[str, Any]]:
        ranked: list[dict[str, Any]] = []
        for record in records:
            score = self._score_record(record, query_tokens)
            if score < self.min_score:
                continue
            ranked.append(self._to_result(record, score, record_type))

        ranked.sort(key=lambda item: item["score"], reverse=True)
        return ranked[:limit]

    def _score_record(self, record: dict[str, Any], query_tokens: list[str]) -> float:
        cached = self._record_score_cache.get(self._build_record_cache_key(record))
        if cached is None:
            cached = self._build_record_score_payload(record)
        title = cached["title"]
        haystack = cached["haystack"]

        hits = 0
        title_hits = 0
        for token in query_tokens:
            if token in haystack:
                hits += 1
                if token in title:
                    title_hits += 1

        if hits == 0:
            return 0.0

        coverage = hits / max(min(len(query_tokens), 8), 1)
        score = coverage * 0.55
        score += min(title_hits, 2) * 0.08
        if cached["has_field_score"]:
            score += 0.05
        if cached["has_detail_score"]:
            score += 0.04
        return min(round(score, 4), 1.0)

    def _to_result(self, record: dict[str, Any], score: float, record_type: str) -> dict[str, Any]:
        identifier = record.get("chunk_id") or record.get("rule_id") or record.get("source_id") or "unknown"
        return {
            "id": identifier,
            "title": record.get("title", ""),
            "content": record.get("content", ""),
            "source": record.get("source_id", ""),
            "score": round(score, 4),
            "record_type": record_type,
            "citation": identifier,
            "url": record.get("url", ""),
            "category": record.get("category", ""),
            "authority": record.get("authority", ""),
        }

    def _deduplicate(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        deduped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in records:
            identifier = record.get("id", "")
            if identifier in seen:
                continue
            seen.add(identifier)
            deduped.append(record)
        return deduped

    def _truncate_context(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        total_chars = 0
        truncated: list[dict[str, Any]] = []
        for record in records:
            content = str(record.get("content", ""))
            remaining = self.max_context_chars - total_chars
            if remaining <= 0:
                break

            if len(content) > remaining:
                clipped = dict(record)
                clipped["content"] = content[: max(remaining - 1, 1)] + "…"
                clipped["content_truncated"] = True
                truncated.append(clipped)
                break

            truncated.append(record)
            total_chars += len(content)
        return truncated

    def _build_synonym_map(self, payload: dict[str, Any]) -> dict[str, set[str]]:
        mapping: dict[str, set[str]] = {}
        for key, values in payload.items():
            group = {str(key).lower()}
            group.update(str(item).lower() for item in values or [])
            for item in group:
                mapping.setdefault(item, set()).update(group)
        return mapping

    def _build_topic_entries(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for topic_name, topic_info in payload.items():
            if not isinstance(topic_info, dict):
                continue
            entries.append(
                {
                    "topic_name": str(topic_name).lower(),
                    "keywords": {str(item).lower() for item in topic_info.get("keywords", []) if str(item).strip()},
                    "rule_ids": [
                        str(item.get("rule_id", "")).strip()
                        for item in topic_info.get("rules", [])
                        if isinstance(item, dict) and str(item.get("rule_id", "")).strip()
                    ],
                }
            )
        return entries

    def _build_rust_payload_cache(
        self,
        records: dict[str, dict[str, Any]],
        record_type: str,
    ) -> dict[str, dict[str, Any]]:
        return {
            record_id: self._build_rust_payload(record, record_type)
            for record_id, record in records.items()
        }

    def _build_rust_payload(self, record: dict[str, Any], record_type: str) -> dict[str, Any]:
        return {
            "id": self._resolve_record_identifier(record),
            "record_type": record_type,
            "title": str(record.get("title", "")),
            "content": str(record.get("content", "")),
            "category": str(record.get("category", "")),
            "rule_type": str(record.get("rule_type", "")),
            "tags": [str(item) for item in (record.get("tags") or []) if str(item).strip()],
            "scenarios": [str(item) for item in (record.get("scenarios") or []) if str(item).strip()],
            "liability_subjects": [str(item) for item in (record.get("liability_subjects") or []) if str(item).strip()],
        }

    def _build_record_score_cache(self) -> dict[str, dict[str, Any]]:
        cache: dict[str, dict[str, Any]] = {}
        for record in list(self._chunk_by_id.values()) + list(self._rule_by_id.values()):
            cache[self._build_record_cache_key(record)] = self._build_record_score_payload(record)
        return cache

    def _build_record_score_payload(self, record: dict[str, Any]) -> dict[str, Any]:
        content = str(record.get("content", "")).lower()
        title = str(record.get("title", "")).lower()
        tags = " ".join(str(item) for item in (record.get("tags") or [])).lower()
        rule_type = str(record.get("rule_type", "")).lower()
        scenarios = " ".join(str(item) for item in (record.get("scenarios") or [])).lower()
        liability_subjects = " ".join(str(item) for item in (record.get("liability_subjects") or [])).lower()
        return {
            "title": title,
            "haystack": " ".join([title, tags, rule_type, scenarios, liability_subjects, content]),
            "has_field_score": bool(record.get("category") or record.get("rule_type")),
            "has_detail_score": bool(record.get("scenarios") or record.get("liability_subjects")),
        }

    def _build_record_cache_key(self, record: dict[str, Any]) -> str:
        if record.get("chunk_id"):
            return f"chunk:{record['chunk_id']}"
        if record.get("rule_id"):
            return f"rule:{record['rule_id']}"
        return f"source:{record.get('source_id', 'unknown')}"

    @staticmethod
    def _resolve_record_identifier(record: dict[str, Any]) -> str:
        return str(record.get("chunk_id") or record.get("rule_id") or record.get("source_id") or "unknown")

    def _resolve_data_dir(
        self,
        manifest_path: Path | None,
        chunks_path: Path | None,
        rules_path: Path | None,
        search_index_path: Path | None,
    ) -> Path:
        for candidate in (manifest_path, chunks_path, rules_path, search_index_path):
            if candidate is not None:
                return candidate.parent.resolve()
        raise ProviderError("无法推断知识库数据目录。")

    def _resolve_manifest_path(self, manifest_path: Path | None) -> Path:
        if manifest_path is not None:
            return manifest_path.resolve()
        return (self.data_dir / "manifest.json").resolve()

    def _resolve_chunks_path(self, chunks_path: Path | None) -> Path:
        if chunks_path is not None:
            return chunks_path.resolve()
        return (self.data_dir / "kbase_chunks.jsonl").resolve()

    def _resolve_rules_path(self, rules_path: Path | None) -> Path:
        if rules_path is not None:
            candidate = rules_path.resolve()
        else:
            candidate = (self.data_dir / "liability_rules.jsonl").resolve()

        if not self.prefer_enhanced_rules:
            return candidate

        enhanced_candidate = candidate.with_name("liability_rules_enhanced.jsonl")
        if enhanced_candidate.exists():
            return enhanced_candidate.resolve()
        return candidate

    def _resolve_search_index_path(self, search_index_path: Path | None) -> Path:
        if search_index_path is not None:
            return search_index_path.resolve()
        return (self.data_dir / "search_index.json").resolve()

    def _has_manifest_changed(self) -> bool:
        if self._manifest_mtime_ns is None:
            return True
        return self._get_mtime_ns(self.manifest_path) != self._manifest_mtime_ns

    @staticmethod
    def _get_mtime_ns(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except FileNotFoundError as exc:
            raise ProviderError(f"本地知识库文件不存在: {path}") from exc

    @staticmethod
    def _new_score_state() -> dict[str, Any]:
        return {
            "match_tokens": set(),
            "tf_sum": 0.0,
            "title_hits": 0,
            "bonus": 0.0,
        }
