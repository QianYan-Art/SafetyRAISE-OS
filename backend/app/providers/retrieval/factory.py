from __future__ import annotations

from app.core.exceptions import ConfigurationError, ProviderError
from app.core.settings import Settings, get_api_key
from app.providers.lmstudio_residency import LMStudioResidencyManager, LMStudioResidencySpec
from app.providers.retrieval.base import BaseRetriever
from app.providers.retrieval.dense_index import DenseIndexStore
from app.providers.retrieval.embedding_client import EmbeddingClient
from app.providers.retrieval.hybrid_retriever import HybridRetriever
from app.providers.retrieval.local_jsonl_retriever import LocalJsonlRetriever
from app.providers.retrieval.mock_retriever import MockRetriever
from app.providers.retrieval.reranker_client import RerankerClient


def build_retriever(
    settings: Settings,
    *,
    lmstudio_residency_manager: LMStudioResidencyManager | None = None,
    embedding_residency_companions: list[LMStudioResidencySpec] | None = None,
) -> BaseRetriever:
    provider = settings.retrieval.provider.strip().lower()
    if provider == "mock":
        return MockRetriever(min_score=settings.retrieval.min_score)

    if provider == "local_jsonl":
        return _build_local_jsonl_with_fallback(settings)

    if provider == "hybrid_local":
        sparse_retriever = _build_local_jsonl_with_fallback(settings)
        if isinstance(sparse_retriever, MockRetriever):
            return sparse_retriever

        embedding_client = None
        reranker_client = None
        dense_index = None
        dependency_errors: list[str] = []

        embedding_cfg = settings.models.retrieval_embedding
        try:
            embedding_client = EmbeddingClient(
                base_url=embedding_cfg.base_url,
                model=embedding_cfg.model,
                api_key=get_api_key(embedding_cfg.api_key_env) if embedding_cfg.api_key_env else "",
                provider_name=embedding_cfg.provider,
                timeout_seconds=embedding_cfg.timeout_seconds,
                lmstudio_host_allowlist=settings.app.lmstudio_host_allowlist,
                lmstudio_ttl_seconds=embedding_cfg.lmstudio_ttl_seconds,
                lmstudio_residency_manager=lmstudio_residency_manager,
                residency_companions=embedding_residency_companions,
                query_instruction=embedding_cfg.query_instruction,
                query_max_length=embedding_cfg.query_max_length,
                document_max_length=embedding_cfg.document_max_length,
                cache_size=embedding_cfg.cache_size,
                cache_ttl_seconds=embedding_cfg.cache_ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            dependency_errors.append(f"embedding 服务初始化失败: {exc}")

        reranker_cfg = settings.models.retrieval_reranker
        try:
            reranker_client = RerankerClient(
                base_url=reranker_cfg.base_url,
                model=reranker_cfg.model,
                api_key=get_api_key(reranker_cfg.api_key_env) if reranker_cfg.api_key_env else "",
                timeout_seconds=reranker_cfg.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            dependency_errors.append(f"reranker 服务初始化失败: {exc}")

        hybrid_cfg = settings.retrieval.hybrid
        try:
            dense_index = DenseIndexStore(
                manifest_path=settings.resolve_path(hybrid_cfg.dense_manifest_path),
                records_path=settings.resolve_path(hybrid_cfg.dense_records_path),
                vectors_path=settings.resolve_path(hybrid_cfg.dense_vectors_path),
                expected_model=settings.models.retrieval_embedding.model,
                expected_dimensions=settings.models.retrieval_embedding.dimensions,
            )
        except Exception as exc:  # noqa: BLE001
            dependency_errors.append(f"稠密索引不可用: {exc}")

        return HybridRetriever(
            sparse_retriever=sparse_retriever,
            embedding_client=embedding_client,
            reranker_client=reranker_client,
            dense_index=dense_index,
            dense_index_version=dense_index.version if dense_index is not None else "",
            embedding_model=settings.models.retrieval_embedding.model,
            reranker_model=settings.models.retrieval_reranker.model,
            initial_config={
                "sparse_top_k_chunks": hybrid_cfg.sparse_top_k_chunks,
                "sparse_top_k_rules": hybrid_cfg.sparse_top_k_rules,
                "dense_top_k_chunks": hybrid_cfg.dense_top_k_chunks,
                "dense_top_k_rules": hybrid_cfg.dense_top_k_rules,
                "rrf_merge_top_k": hybrid_cfg.rrf_merge_top_k,
                "rerank_top_k": hybrid_cfg.rerank_top_k,
                "final_context_top_k": hybrid_cfg.final_context_top_k,
                "max_context_chars": hybrid_cfg.max_context_chars,
            },
            agentic_config={
                "sparse_top_k_chunks": hybrid_cfg.agentic_sparse_top_k_chunks,
                "sparse_top_k_rules": hybrid_cfg.agentic_sparse_top_k_rules,
                "dense_top_k_chunks": hybrid_cfg.agentic_dense_top_k_chunks,
                "dense_top_k_rules": hybrid_cfg.agentic_dense_top_k_rules,
                "rrf_merge_top_k": hybrid_cfg.agentic_rrf_merge_top_k,
                "rerank_top_k": hybrid_cfg.agentic_rerank_top_k,
                "final_context_top_k": hybrid_cfg.agentic_rerank_top_k,
                "max_context_chars": hybrid_cfg.max_context_chars,
            },
            fallback_reason="；".join(dependency_errors) if dependency_errors else None,
        )

    raise ConfigurationError(f"不支持的检索提供器: {settings.retrieval.provider}")


def _build_local_jsonl_with_fallback(settings: Settings) -> BaseRetriever:
    local_cfg = settings.retrieval.local_jsonl
    try:
        return LocalJsonlRetriever(
            manifest_path=settings.resolve_path(local_cfg.manifest_path),
            chunks_path=settings.resolve_path(local_cfg.chunks_path),
            rules_path=settings.resolve_path(local_cfg.rules_path),
            search_index_path=(
                settings.resolve_path(local_cfg.search_index_path)
                if local_cfg.search_index_path
                else None
            ),
            min_score=settings.retrieval.min_score,
            top_k_chunks=local_cfg.top_k_chunks,
            top_k_rules=local_cfg.top_k_rules,
            max_context_chars=local_cfg.max_context_chars,
            prefer_enhanced_rules=local_cfg.prefer_enhanced_rules,
            enable_search_index=local_cfg.enable_search_index,
            watch_manifest_changes=local_cfg.watch_manifest_changes,
        )
    except Exception as exc:  # noqa: BLE001
        if not local_cfg.fallback_mock_on_error:
            raise ProviderError(
                "知识库当前不可用，请检查检索数据目录后重试。",
                details={"error": str(exc)},
            ) from exc
        return MockRetriever(
            min_score=settings.retrieval.min_score,
            degraded=True,
            fallback_reason=str(exc),
        )
