from abc import ABC, abstractmethod
from typing import Any


class BaseInputAdapter(ABC):
    @abstractmethod
    def load(self) -> dict[str, Any]:
        raise NotImplementedError
