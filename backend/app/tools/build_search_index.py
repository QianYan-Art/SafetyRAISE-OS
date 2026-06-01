from __future__ import annotations

import argparse
import ctypes
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from app.core.rust_accel import load_rust_token_accel


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="为 SafetyRAISE 生成 search_index.json。")
    parser.add_argument("--manifest", required=True, help="manifest.json 路径")
    parser.add_argument("--chunks", required=True, help="kbase_chunks.jsonl 路径")
    parser.add_argument("--rules", required=True, help="liability_rules.jsonl 路径")
    parser.add_argument("--output", required=True, help="输出 search_index.json 路径")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            payload = json.loads(stripped)
            if not isinstance(payload, dict):
                raise ValueError(f"{path}:{line_no} 不是 JSON 对象。")
            rows.append(payload)
    return rows


def tokenize_many(texts: list[str]) -> list[list[str]]:
    rust_accel = load_rust_token_accel()
    if rust_accel is not None:
        try:
            payload = json.dumps(texts, ensure_ascii=False)
            raw_ptr = rust_accel.accel_tokenize_batch(payload.encode("utf-8"))
            if raw_ptr:
                try:
                    raw_payload = ctypes.string_at(raw_ptr).decode("utf-8")
                finally:
                    rust_accel.accel_free_string(raw_ptr)
                parsed = json.loads(raw_payload)
                if isinstance(parsed, list):
                    return [
                        [str(token).strip().lower() for token in (row or []) if str(token).strip()]
                        for row in parsed
                    ]
        except Exception:  # noqa: BLE001
            pass
    return [tokenize_text_fallback(text) for text in texts]


def tokenize_text_fallback(text: str) -> list[str]:
    import re

    normalized = re.sub(r"\s+", " ", text).strip().lower()
    segments = re.findall(r"[\u4e00-\u9fff]{2,}|[a-z0-9_.:/-]{2,}", normalized)
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
    tokens: set[str] = set()
    for segment in segments:
        if segment in stop_words:
            continue
        if all("\u4e00" <= char <= "\u9fff" for char in segment):
            tokens.add(segment)
            if len(segment) > 4:
                for size in (4, 3, 2):
                    for index in range(len(segment) - size + 1):
                        token = segment[index : index + size]
                        if token not in stop_words:
                            tokens.add(token)
        elif not segment.isdigit():
            tokens.add(segment)
    return sorted(token for token in tokens if len(token) >= 2)


def build_record_text(record: dict[str, Any], record_type: str) -> str:
    parts: list[str] = [str(record.get("title") or "").strip(), str(record.get("content") or "").strip()]
    if record_type == "chunk":
        parts.extend(str(item).strip() for item in (record.get("tags") or []) if str(item).strip())
    else:
        parts.extend(str(item).strip() for item in (record.get("scenarios") or []) if str(item).strip())
        parts.extend(str(item).strip() for item in (record.get("liability_subjects") or []) if str(item).strip())
        parts.append(str(record.get("rule_type") or "").strip())
        parts.append(str(record.get("category") or "").strip())
    return "\n".join(part for part in parts if part)


def build_inverted_index(records: list[dict[str, Any]], record_type: str) -> dict[str, list[dict[str, Any]]]:
    texts = [build_record_text(record, record_type) for record in records]
    token_rows = tokenize_many(texts)
    inverted: dict[str, list[dict[str, Any]]] = defaultdict(list)
    identifier_key = "chunk_id" if record_type == "chunk" else "rule_id"

    for record, tokens in zip(records, token_rows, strict=False):
        record_id = str(record.get(identifier_key) or "").strip()
        if not record_id:
            continue
        counter = Counter(tokens)
        title = str(record.get("title") or "").strip()
        for token, tf in counter.items():
            inverted[token].append({"id": record_id, "tf": int(tf), "title": title})

    return {token: postings for token, postings in inverted.items()}


def build_topic_index(rules: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    topics: dict[str, dict[str, Any]] = {}
    for rule in rules:
        topic_name = str(rule.get("category") or rule.get("rule_type") or "").strip()
        rule_id = str(rule.get("rule_id") or "").strip()
        if not topic_name or not rule_id:
            continue
        topic_entry = topics.setdefault(topic_name, {"keywords": [], "rules": []})
        topic_entry["rules"].append({"rule_id": rule_id})
        keywords = tokenize_text_fallback(topic_name)
        topic_entry["keywords"] = sorted(set(topic_entry["keywords"]) | set(keywords))
    return topics


def build_relation_index(chunks: list[dict[str, Any]], rules: list[dict[str, Any]]) -> dict[str, dict[str, list[str]]]:
    rules_by_source: dict[str, list[str]] = defaultdict(list)
    for rule in rules:
        source = str(rule.get("source_id") or "").strip()
        rule_id = str(rule.get("rule_id") or "").strip()
        if source and rule_id:
            rules_by_source[source].append(rule_id)

    case_to_rules: dict[str, list[str]] = {}
    for chunk in chunks:
        chunk_id = str(chunk.get("chunk_id") or "").strip()
        source = str(chunk.get("source_id") or "").strip()
        if chunk_id and source and source in rules_by_source:
            case_to_rules[chunk_id] = rules_by_source[source][:8]
    return {"case_to_rules": case_to_rules}


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest).resolve()
    chunks_path = Path(args.chunks).resolve()
    rules_path = Path(args.rules).resolve()
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    manifest = load_json(manifest_path)
    chunks = load_jsonl(chunks_path)
    rules = load_jsonl(rules_path)

    payload = {
        "generated_at": manifest.get("generated_at"),
        "indexes": {
            "chunk_inverted": build_inverted_index(chunks, "chunk"),
            "rule_inverted": build_inverted_index(rules, "rule"),
        },
        "topic_index": build_topic_index(rules),
        "relation_index": build_relation_index(chunks, rules),
        "synonyms": {},
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "ok",
                "output": str(output_path),
                "chunk_terms": len(payload["indexes"]["chunk_inverted"]),
                "rule_terms": len(payload["indexes"]["rule_inverted"]),
                "topic_count": len(payload["topic_index"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
