from typing import Any

from app.providers.retrieval.base import BaseRetriever


class MockRetriever(BaseRetriever):
    def __init__(
        self,
        min_score: float = 0.2,
        degraded: bool = False,
        fallback_reason: str | None = None,
    ):
        self.min_score = min_score
        self.metadata = {
            "provider": "mock",
            "retrieval_degraded": degraded,
        }
        if fallback_reason:
            self.metadata["fallback_reason"] = fallback_reason

    def retrieve(self, accident_data: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
        title = accident_data.get("事故标题", "事故信息")
        snippet = {
            "id": "mock_kb_001",
            "title": "道路交通事故分析通用框架",
            "content": f"针对“{title}”，建议按事实、证据、研判、建议四层展开。",
            "source": "mock_retriever",
            "score": 0.75,
        }
        return [snippet][:top_k]

    @property
    def supports_query_search(self) -> bool:
        return True

    def search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        snippet = {
            "id": f"mock_query_{abs(hash(query)) % 10000:04d}",
            "title": "知识库检索兜底结果",
            "content": f"围绕“{query}”建议优先核对适用法条、责任规则与同类案例。",
            "source": "mock_retriever",
            "score": 0.68,
            "record_type": "mock",
            "citation": f"mock:{query}",
        }
        return [snippet][:top_k]
