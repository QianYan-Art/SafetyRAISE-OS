import asyncio
from types import SimpleNamespace

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.knowledge_bridge import KnowledgeBridge, MeteredEmbedding


class Transport:
    def __init__(self, *, cached=False, error=None):
        self.calls = []
        self.cached, self.error = cached, error
        self.response = {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}

    async def replay(self, role, payload):
        self.calls.append(("replay", role, payload))
        return self.response if self.cached else None

    async def request(self, role, payload):
        self.calls.append(("request", role, payload))
        if self.error:
            raise HarnessError(self.error)
        return self.response


@pytest.mark.parametrize("cached", [False, True])
def test_embedding_reuses_settled_result_and_preserves_query_format(cached):
    async def run():
        transport = Transport(cached=cached)
        embedding = MeteredEmbedding(
            transport, model="synthetic", dimensions=2,
            query_instruction="查找依据", query_max_length=100,
        )
        result = await asyncio.to_thread(embedding.embed_query, "合成查询")
        assert result == [0.1, 0.2]
        assert [call[0] for call in transport.calls] == (
            ["replay"] if cached else ["replay", "request"]
        )
        assert transport.calls[0][2] == {
            "model": "synthetic", "input": ["Instruct: 查找依据\nQuery: 合成查询"],
        }
        embedding.close()
        with pytest.raises(HarnessError, match="lease_lost"):
            await asyncio.to_thread(embedding.embed_query, "关闭后查询")

    asyncio.run(run())


def test_embedding_unknown_completion_is_not_silently_degraded():
    async def run():
        embedding = MeteredEmbedding(
            Transport(error="completion_unknown"), model="synthetic",
            dimensions=2, query_instruction="", query_max_length=100,
        )
        with pytest.raises(HarnessError, match="completion_unknown"):
            await asyncio.to_thread(embedding.embed_query, "合成")
        embedding.close()

    asyncio.run(run())


def test_embedding_rejects_wrong_dimensions_and_event_loop_blocking():
    async def run():
        embedding = MeteredEmbedding(
            Transport(), model="synthetic", dimensions=3,
            query_instruction="", query_max_length=100,
        )
        with pytest.raises(HarnessError, match="embedding_requires_worker_thread"):
            embedding.embed_query("合成")
        with pytest.raises(HarnessError, match="invalid_embedding_response"):
            await asyncio.to_thread(embedding.embed_query, "合成")
        embedding.close()

    asyncio.run(run())


def test_bridge_uses_initial_ranking_and_returns_approved_full_text():
    calls, guards = [], []
    retriever = SimpleNamespace(
        _hybrid_available=True,
        initial_config={"final_context_top_k": 3},
        agentic_config={"final_context_top_k": 2},
        _run_hybrid=lambda **kwargs: calls.append(kwargs) or [
            {"id": "rule-1", "content": "旧检索器可能截短的文本"},
        ],
    )
    chunk = {"id": "rule-1", "text": "冻结完整原文"}
    bridge = KnowledgeBridge(retriever, (chunk,), validate_source=lambda: guards.append(True))
    assert bridge.initial("事故", 3) == [chunk]
    assert calls[-1]["enforce_type_balance"] is True
    assert calls[-1]["retrieval_mode"] == "initial"
    assert bridge.search("查询", 3) == [chunk]
    assert calls[-1]["final_limit"] == 2
    assert calls[-1]["enforce_type_balance"] is False
    assert len(guards) == 4


def test_bridge_rejects_unapproved_source():
    retriever = SimpleNamespace(
        _hybrid_available=True, initial_config={"final_context_top_k": 3},
        _run_hybrid=lambda **kwargs: [{"id": "unknown"}],
    )
    bridge = KnowledgeBridge(retriever, (), validate_source=lambda: None)
    with pytest.raises(HarnessError, match="knowledge_source_mismatch"):
        bridge.initial("事故", 3)
