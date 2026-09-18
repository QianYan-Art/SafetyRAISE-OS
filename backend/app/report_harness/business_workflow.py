from dataclasses import dataclass

from app.providers.retrieval.hybrid_retriever import HybridRetriever


@dataclass(frozen=True)
class BusinessWorkflow:
    """旧业务语义的显式接入合同，不改变合成测试或历史运行。"""

    expert_prompt: str
    report_prompt: str
    initial_top_k: int = 3
    additional_rounds: int = 2
    additional_top_k: int = 3
    max_total_snippets: int = 9
    max_query_chars: int = 120

    def __post_init__(self):
        if not self.expert_prompt.strip() or not self.report_prompt.strip():
            raise ValueError("完整业务运行必须提供原专家和报告模板。")
        for value, minimum, maximum in (
            (self.initial_top_k, 1, 10),
            (self.additional_rounds, 0, 8),
            (self.additional_top_k, 1, 10),
            (self.max_total_snippets, 1, 100),
            (self.max_query_chars, 1, 1000),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("业务检索预算无效。")
        if self.initial_top_k > self.max_total_snippets:
            raise ValueError("首轮检索不能超过总片段预算。")

    def initial_query(self, accident_data: dict) -> str:
        query = HybridRetriever._build_initial_query(accident_data)
        if not query:
            raise ValueError("事故信息没有可用于首轮检索的内容。")
        return query

    def prompts(self) -> dict[str, str]:
        return {"expert": self.expert_prompt, "report": self.report_prompt}

    def retrieval_policy(self) -> dict:
        return {
            "initial_top_k": self.initial_top_k,
            "additional_rounds": self.additional_rounds,
            "additional_top_k": self.additional_top_k,
            "max_total_snippets": self.max_total_snippets,
            "max_query_chars": self.max_query_chars,
        }
