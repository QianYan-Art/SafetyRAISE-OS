from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LLMToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str | None = None
    raw_arguments: str | None = None


@dataclass(slots=True)
class LLMGenerateResult:
    content: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    reasoning_content: str = ""
    response_metadata: dict[str, Any] = field(default_factory=dict)


class BaseLLMProvider(ABC):
    @abstractmethod
    def generate(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def health_check(self) -> bool:
        raise NotImplementedError

    @property
    def supports_tool_calling(self) -> bool:
        return False

    def generate_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, Any]],
    ) -> LLMGenerateResult:
        raise NotImplementedError("当前模型提供器未实现原生 tool calling。")

    def close(self) -> None:
        return None
