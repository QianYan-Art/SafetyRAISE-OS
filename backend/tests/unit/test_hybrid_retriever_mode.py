import pytest

from app.providers.retrieval.hybrid_retriever import HybridRetriever


def build_retriever(*, dense: bool, reranker: bool) -> HybridRetriever:
    return HybridRetriever(
        sparse_retriever=object(),
        embedding_client=object() if dense else None,
        reranker_client=object() if reranker else None,
        dense_index=object() if dense else None,
        dense_index_version="v1",
        embedding_model="synthetic-embedding",
        reranker_model="synthetic-reranker" if reranker else None,
        initial_config={},
        agentic_config={},
    )


@pytest.mark.parametrize(
    ("dense", "reranker", "mode", "degraded"),
    [
        (True, True, "hybrid", False),
        # reranker 按部署默认停用时，稠密与稀疏 RRF 融合是正常运行，不应在初始元数据里报降级。
        (True, False, "hybrid_rrf_only", False),
        (False, False, "sparse_only_fallback", True),
    ],
)
def test_initial_metadata_uses_same_degraded_rule_as_search(dense, reranker, mode, degraded):
    metadata = build_retriever(dense=dense, reranker=reranker).metadata
    assert metadata["mode"] == mode
    assert metadata["retrieval_degraded"] is degraded
