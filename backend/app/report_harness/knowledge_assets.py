from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from app.providers.retrieval.dense_index import DenseIndexStore
from app.providers.retrieval.factory import _build_local_jsonl_with_fallback
from app.providers.retrieval.hybrid_retriever import HybridRetriever
from app.report_harness.authorization import KnowledgeCollection
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError


def file_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identity(path: Path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@dataclass
class KnowledgeAssets:
    sparse: object
    dense: object
    collection: KnowledgeCollection
    chunks: tuple[dict, ...]
    identities: dict[Path, tuple]
    embedding_model: str
    initial_config: dict
    agentic_config: dict

    def validate(self) -> None:
        try:
            if any(_identity(path) != identity for path, identity in self.identities.items()):
                raise HarnessError("authorization_stale")
        except OSError as exc:
            raise HarnessError("knowledge_dependencies_unavailable", 503) from exc

    def retriever(self, embedding) -> HybridRetriever:
        self.validate()
        return HybridRetriever(
            sparse_retriever=self.sparse, embedding_client=embedding,
            reranker_client=None, dense_index=self.dense, dense_index_version=self.dense.version,
            embedding_model=self.embedding_model, reranker_model="",
            initial_config=dict(self.initial_config), agentic_config=dict(self.agentic_config),
        )


def load_knowledge_assets(settings, *, approved_content_digest: str) -> KnowledgeAssets:
    """只从固定配置读取已批准资产；不下载、不构建索引、不自动回退演示知识。"""
    if settings.retrieval.provider != "hybrid_local" or settings.models.retrieval_reranker.enabled:
        raise HarnessError("knowledge_profile_unapproved")
    local, hybrid = settings.retrieval.local_jsonl, settings.retrieval.hybrid
    named_paths = {
        "manifest": local.manifest_path, "chunks": local.chunks_path,
        "rules": local.rules_path, "search_index": local.search_index_path,
        "dense_manifest": hybrid.dense_manifest_path, "dense_records": hybrid.dense_records_path,
        "dense_vectors": hybrid.dense_vectors_path,
    }
    if any(not value for value in named_paths.values()):
        raise HarnessError("knowledge_profile_unapproved")
    paths = {name: Path(settings.resolve_path(value)).resolve() for name, value in named_paths.items()}
    try:
        identities = {path: _identity(path) for path in paths.values()}
        content_digest = canonical_digest({name: file_digest(path) for name, path in paths.items()})
    except OSError as exc:
        raise HarnessError("knowledge_dependencies_unavailable", 503) from exc
    if content_digest != approved_content_digest:
        raise HarnessError("knowledge_source_unapproved")
    guarded = settings.model_copy(deep=True)
    guarded.retrieval.local_jsonl.fallback_mock_on_error = False
    guarded.retrieval.local_jsonl.watch_manifest_changes = False
    sparse = _build_local_jsonl_with_fallback(guarded)
    for name, attribute in (
        ("manifest", "manifest_path"), ("chunks", "chunks_path"),
        ("rules", "rules_path"), ("search_index", "search_index_path"),
    ):
        if Path(getattr(sparse, attribute)).resolve() != paths[name]:
            raise HarnessError("knowledge_source_unapproved")
    sparse._ensure_loaded()
    dense = DenseIndexStore(
        manifest_path=paths["dense_manifest"], records_path=paths["dense_records"],
        vectors_path=paths["dense_vectors"],
        expected_model=settings.models.retrieval_embedding.model,
        expected_dimensions=settings.models.retrieval_embedding.dimensions,
    )
    collection = KnowledgeCollection(
        collection_id="safetyraise-kbase",
        version=str(sparse._manifest.get("catalog_version") or content_digest),
        content_digest=content_digest, label="原项目固定版本知识库",
    )
    manifest_digest = canonical_digest([collection.model_dump(mode="json")])
    chunks = []
    ids = set()
    for record in [*sparse._chunk_records, *sparse._rule_records]:
        identifier = record.get("chunk_id") or record.get("rule_id") or record.get("source_id")
        text = record.get("content")
        if not isinstance(identifier, str) or not isinstance(text, str) or not text.strip():
            raise HarnessError("knowledge_record_invalid")
        if identifier in ids:
            raise HarnessError("knowledge_record_duplicate")
        ids.add(identifier)
        source = str(record.get("source_id") or identifier)
        original = "\n".join((
            "标题：" + str(record.get("title") or ""),
            "来源：" + source, "网址：" + str(record.get("url") or ""), text,
        ))
        chunks.append({
            "id": identifier, "document_id": source, "version": collection.version,
            "text": original, "digest": canonical_digest(original),
            "manifest_digest": manifest_digest,
        })
    if not chunks:
        raise HarnessError("knowledge_dependencies_unavailable", 503)
    fields = (
        "sparse_top_k_chunks", "sparse_top_k_rules", "dense_top_k_chunks", "dense_top_k_rules",
        "rrf_merge_top_k", "rerank_top_k", "final_context_top_k", "max_context_chars",
    )
    initial = {name: getattr(hybrid, name) for name in fields}
    agentic = {name: getattr(hybrid, "agentic_" + name) for name in fields[:-2]}
    agentic.update(final_context_top_k=hybrid.agentic_rerank_top_k,
                   max_context_chars=hybrid.max_context_chars)
    assets = KnowledgeAssets(
        sparse, dense, collection, tuple(chunks), identities,
        settings.models.retrieval_embedding.model, initial, agentic,
    )
    assets.validate()
    return assets
