from typing import Any, TypedDict


class WorkflowState(TypedDict, total=False):
    trace_id: str
    session_id: str
    accident_raw: dict[str, Any]
    accident_validated: dict[str, Any]
    guidance_prompt: str
    guidance_raw: str
    guidance_json: dict[str, Any]
    initial_knowledge_snippets: list[dict[str, Any]]
    knowledge_snippets: list[dict[str, Any]]
    retrieval_meta: dict[str, Any]
    agentic_retrieval_rounds: list[dict[str, Any]]
    report_prompt: str
    report_raw: str
    report_output: dict[str, Any]
    output_dir: str
    errors: list[str]
