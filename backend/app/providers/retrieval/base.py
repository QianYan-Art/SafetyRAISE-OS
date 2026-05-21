from abc import ABC, abstractmethod
from typing import Any


class BaseRetriever(ABC):
    @abstractmethod
    def retrieve(self, accident_data: dict[str, Any], top_k: int) -> list[dict[str, Any]]:
        raise NotImplementedError

    @property
    def supports_query_search(self) -> bool:
        return False

    def search(self, query: str, top_k: int) -> list[dict[str, Any]]:
        return []
