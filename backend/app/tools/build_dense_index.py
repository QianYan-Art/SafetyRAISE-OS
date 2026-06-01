from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="为 SafetyRAISE 知识库生成 dense 索引三件套。")
    parser.add_argument("--manifest", required=True, help="manifest.json 路径")
    parser.add_argument("--chunks", required=True, help="kbase_chunks.jsonl 路径")
    parser.add_argument("--rules", required=True, help="liability_rules.jsonl 路径")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    parser.add_argument("--embedding-base-url", required=True, help="embedding 服务根地址，例如 https://<MODEL_API_HOST>/v1")
    parser.add_argument("--embedding-model", required=True, help="embedding 模型名")
    parser.add_argument("--api-key", default="", help="embedding API Key，留空则继续尝试环境变量或 key 文件")
    parser.add_argument("--api-key-env", default="", help="embedding API Key 对应环境变量名")
    parser.add_argument("--key-file", default="", help="可选：密钥文件路径。若文件仅两行纯文本，则默认取第 2 行作为千言 Key")
    parser.add_argument("--key-file-line", type=int, default=2, help="key 文件取值行号，从 1 开始，默认第 2 行")
    parser.add_argument("--batch-size", type=int, default=16, help="批量 embedding 大小")
    parser.add_argument("--timeout-seconds", type=int, default=120, help="embedding 请求超时秒数")
    return parser.parse_args()


def resolve_api_key(args: argparse.Namespace) -> str:
    if args.api_key.strip():
        return args.api_key.strip()
    if args.api_key_env.strip():
        value = os.getenv(args.api_key_env.strip(), "").strip()
        if value:
            return value
    if args.key_file.strip():
        lines = [
            line.strip()
            for line in Path(args.key_file).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        line_index = max(args.key_file_line - 1, 0)
        if line_index < len(lines):
            raw = lines[line_index]
            for separator in ("=", ":", "："):
                if separator in raw:
                    return raw.split(separator, 1)[1].strip()
            return raw
    return ""


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


def build_dense_records(
    *,
    chunks: list[dict[str, Any]],
    rules: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for chunk in chunks:
        record = dict(chunk)
        record["record_type"] = "chunk"
        record["id"] = str(chunk.get("chunk_id") or chunk.get("id") or "").strip()
        records.append(record)
    for rule in rules:
        record = dict(rule)
        record["record_type"] = "rule"
        record["id"] = str(rule.get("rule_id") or rule.get("id") or "").strip()
        records.append(record)
    return records


def build_embedding_text(record: dict[str, Any]) -> str:
    parts: list[str] = []
    title = str(record.get("title") or "").strip()
    content = str(record.get("content") or "").strip()
    category = str(record.get("category") or record.get("rule_type") or "").strip()
    tags = record.get("tags") or record.get("scenarios") or []
    authority = str(record.get("authority") or "").strip()

    if record.get("record_type") == "chunk":
        parts.append("类型: chunk")
    else:
        parts.append("类型: rule")
    if title:
        parts.append(f"标题: {title}")
    if category:
        parts.append(f"类别: {category}")
    if tags:
        normalized_tags = [str(item).strip() for item in tags if str(item).strip()]
        if normalized_tags:
            parts.append("标签: " + " ".join(normalized_tags))
    if authority:
        parts.append(f"依据: {authority}")
    if content:
        parts.append("内容: " + content)
    return "\n".join(parts).strip()


def build_embeddings_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/embeddings"):
        return normalized
    if normalized.endswith("/v1"):
        return normalized + "/embeddings"
    return normalized + "/v1/embeddings"


def fetch_embeddings(
    *,
    base_url: str,
    model: str,
    api_key: str,
    texts: list[str],
    batch_size: int,
    timeout_seconds: int,
) -> np.ndarray:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    vectors: list[list[float]] = []
    with httpx.Client(timeout=timeout_seconds) as client:
        for offset in range(0, len(texts), batch_size):
            batch = texts[offset: offset + batch_size]
            response = client.post(
                build_embeddings_url(base_url),
                headers=headers,
                json={
                    "model": model,
                    "input": batch,
                    "encoding_format": "float",
                },
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("data")
            if not isinstance(items, list) or len(items) != len(batch):
                raise ValueError("embedding 服务返回的 data 数量异常。")
            vectors.extend(
                [float(value) for value in item["embedding"]]
                for item in items
            )
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized = matrix / norms
    return normalized.astype(np.float16)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    args = parse_args()
    manifest_path = Path(args.manifest).resolve()
    chunks_path = Path(args.chunks).resolve()
    rules_path = Path(args.rules).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_json(manifest_path)
    chunks = load_jsonl(chunks_path)
    rules = load_jsonl(rules_path)
    records = build_dense_records(chunks=chunks, rules=rules)
    texts = [build_embedding_text(record) for record in records]
    api_key = resolve_api_key(args)

    vectors = fetch_embeddings(
        base_url=args.embedding_base_url,
        model=args.embedding_model,
        api_key=api_key,
        texts=texts,
        batch_size=max(int(args.batch_size), 1),
        timeout_seconds=max(int(args.timeout_seconds), 30),
    )

    dense_manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "embedding_model": args.embedding_model,
        "embedding_base_url": args.embedding_base_url,
        "dimensions": int(vectors.shape[1]),
        "record_count": len(records),
        "sources": {
            "manifest_path": str(manifest_path),
            "chunks_path": str(chunks_path),
            "rules_path": str(rules_path),
        },
        "source_manifest_generated_at": manifest.get("generated_at"),
        "source_catalog_version": ((manifest.get("catalog_meta") or {}).get("catalog_version")),
    }

    dense_manifest_path = output_dir / "dense_manifest.json"
    dense_records_path = output_dir / "dense_records.jsonl"
    dense_vectors_path = output_dir / "dense_vectors.f16.npy"

    dense_manifest_path.write_text(
        json.dumps(dense_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_jsonl(dense_records_path, records)
    np.save(dense_vectors_path, vectors, allow_pickle=False)

    print(
        json.dumps(
            {
                "status": "ok",
                "record_count": len(records),
                "dimensions": int(vectors.shape[1]),
                "dense_manifest_path": str(dense_manifest_path),
                "dense_records_path": str(dense_records_path),
                "dense_vectors_path": str(dense_vectors_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
