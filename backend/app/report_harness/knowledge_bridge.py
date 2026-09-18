from __future__ import annotations

import asyncio
import math
from copy import deepcopy
from threading import Lock

from app.report_harness.errors import HarnessError


class MeteredEmbedding:
    """保留原查询格式；检索线程的每次外发由主事件循环统一登记和结算。"""

    def __init__(self, transport, *, model: str, dimensions: int,
                 query_instruction: str, query_max_length: int):
        self.transport = transport
        self.model, self.dimensions = model, dimensions
        self.query_instruction, self.query_max_length = query_instruction, query_max_length
        self.loop = asyncio.get_running_loop()
        self._pending = set()
        self._lock = Lock()
        self._closed = False

    async def _embed(self, payload):
        response = await self.transport.replay("embedding", payload)
        if response is None:
            response = await self.transport.request("embedding", payload)
        try:
            data = response["data"]
            if len(data) != 1 or data[0]["index"] != 0:
                raise ValueError("嵌入数量或顺序不符。")
            vector = data[0]["embedding"]
            if (not isinstance(vector, list) or len(vector) != self.dimensions
                    or any(type(value) not in {float, int} or not math.isfinite(value)
                           for value in vector)):
                raise ValueError("嵌入向量维度或数值无效。")
            return vector
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise HarnessError("invalid_embedding_response") from exc

    def embed_query(self, query: str) -> list[float]:
        try:
            if asyncio.get_running_loop() is self.loop:
                raise HarnessError("embedding_requires_worker_thread")
        except RuntimeError:
            pass
        query = query.strip()[:self.query_max_length]
        if not query:
            raise HarnessError("invalid_embedding_query")
        formatted = (f"Instruct: {self.query_instruction}\nQuery: {query}"
                     if self.query_instruction else query)
        payload = {"model": self.model, "input": [formatted]}
        with self._lock:
            if self._closed:
                raise HarnessError("lease_lost")
            future = asyncio.run_coroutine_threadsafe(self._embed(payload), self.loop)
            self._pending.add(future)
        try:
            return future.result()
        finally:
            with self._lock:
                self._pending.discard(future)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            pending = list(self._pending)
        for future in pending:
            future.cancel()


class KnowledgeBridge:
    """复用旧混合检索排序，失败显式传出，不能吞掉未知费用并悄悄降级。"""

    def __init__(self, retriever, chunks: tuple[dict, ...], *, validate_source):
        self.retriever = retriever
        self.chunks = {item["id"]: item for item in chunks}
        self.validate_source = validate_source

    def _search(self, query, top_k, *, initial):
        self.validate_source()
        if not self.retriever._hybrid_available:
            raise HarnessError("knowledge_dependencies_unavailable", 503)
        config = self.retriever.initial_config if initial else self.retriever.agentic_config
        result = self.retriever._run_hybrid(
            query=query, final_limit=min(top_k, config["final_context_top_k"]),
            config=config, enforce_type_balance=initial,
            retrieval_mode="initial" if initial else "agentic",
        )
        self.validate_source()
        identifiers = [item["id"] for item in result]
        if len(identifiers) != len(set(identifiers)) or any(
            identifier not in self.chunks for identifier in identifiers
        ):
            raise HarnessError("knowledge_source_mismatch")
        return [deepcopy(self.chunks[identifier]) for identifier in identifiers]

    def initial(self, query: str, top_k: int) -> list[dict]:
        return self._search(query, top_k, initial=True)

    def search(self, query: str, top_k: int) -> list[dict]:
        return self._search(query, top_k, initial=False)
