from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pydantic import Field, ValidationError, model_validator

from app.report_harness.contracts import CandidateReport, ReviewResult, canonical_digest
from app.report_harness.errors import HarnessError
from app.schemas.base import StrictModel


def tool_schemas(retrieval_constraints: dict | None = None) -> list[dict]:
    """角色只获四个只读动作，不暴露执行环境或发布操作。"""
    ids = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10}
    definitions = [
        ("list_evidence", {"cursor": {"type": "string"}}, []),
        ("read_evidence", {"evidence_ids": ids, "cursor": {"type": "string"}}, ["evidence_ids"]),
        ("search_knowledge", {
            "query": {"type": "string", "minLength": 1, "maxLength": 1000},
            "top_k": {"type": "integer", "minimum": 1, "maximum": 10},
        }, ["query", "top_k"]),
        ("read_knowledge", {"chunk_ids": ids, "cursor": {"type": "string"}}, ["chunk_ids"]),
    ]
    schemas = [{"name": name, "parameters": {
        "type": "object", "properties": properties, "required": required,
        "additionalProperties": False,
    }} for name, properties, required in definitions]
    if retrieval_constraints is not None:
        constraints = retrieval_constraints
        if constraints["remaining_rounds"] <= 0 or constraints["remaining_snippets"] <= 0:
            return [item for item in schemas if item["name"] != "search_knowledge"]
        search = next(item for item in schemas if item["name"] == "search_knowledge")
        properties = search["parameters"]["properties"]
        properties["top_k"]["maximum"] = min(
            properties["top_k"]["maximum"], constraints["additional_top_k"],
            constraints["remaining_snippets"],
        )
        properties["query"]["maxLength"] = min(
            properties["query"]["maxLength"], constraints["max_query_chars"],
        )
        search["description"] = (
            f"本角色剩余{constraints['remaining_rounds']}次检索；"
            f"最多再返回{constraints['remaining_snippets']}个检索条目。"
        )
    return schemas


class ToolCall(StrictModel):
    call_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any]


class ToolTurn(StrictModel):
    tool_calls: list[ToolCall] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique_calls(self) -> "ToolTurn":
        if len({call.call_id for call in self.tool_calls}) != len(self.tool_calls):
            raise ValueError("同一模型回合的工具调用 ID 不可重复。")
        return self


def normalize_tool_response(response: dict) -> dict:
    """只兼容精确的单工具对象；仍由原工具白名单和参数校验决定是否执行。"""
    if set(response) == {"call_id", "name", "arguments"}:
        call = ToolCall.model_validate(response)
        return {"tool_calls": [call.model_dump(mode="json")]}
    return response


class RoleLoop:
    """有界的角色/工具交互；逻辑轮次计数不是物理请求计费账本。"""

    def __init__(self, tools, checkpoint: Callable[[dict, dict], None], *,
                 before_call: Callable[[], None], max_model_turns=24, max_tool_calls=24,
                 journal=None, replay_model=None, max_protocol_repairs=2):
        if type(max_protocol_repairs) is not int or not 0 <= max_protocol_repairs <= 2:
            raise ValueError("单回合格式修复最多两次。")
        self.tools, self.checkpoint, self.before_call = tools, checkpoint, before_call
        self.journal = journal
        self.replay_model = replay_model
        self.max_model_turns, self.max_tool_calls = max_model_turns, max_tool_calls
        self.max_protocol_repairs = max_protocol_repairs
        self.model_turns = 0
        self.tool_calls = 0

    async def initial_retrieval(self, query: str, top_k: int) -> dict:
        """首检是程序步骤，不依赖模型选择；恢复复用原文和工具状态。"""
        self.before_call()

        async def invoke():
            result = await asyncio.to_thread(self.tools.initial_retrieval, query, top_k)
            return {"result": result, "tool_state": self.tools.checkpoint_state()}

        request = {
            "step": "initial_retrieval", "role": "generator", "name": "search_knowledge",
            "call_id": "controller-initial-retrieval",
            "arguments": {"query": query, "top_k": top_k},
        }
        if self.journal is not None:
            saved = await self.journal.invoke("tool", request, invoke, limit=self.max_tool_calls)
            self.tool_calls = self.journal.attempts("tool")
        else:
            if self.tool_calls >= self.max_tool_calls:
                raise HarnessError("tool_budget_exhausted")
            self.tool_calls += 1
            self.checkpoint({"name": "search_knowledge", "status": "intent",
                             "step": "initial_retrieval"}, request)
            saved = await invoke()
            self.checkpoint({"name": "search_knowledge", "status": "completed",
                             "step": "initial_retrieval",
                             "result_digest": canonical_digest(saved["result"])}, saved)
        self.tools.restore_checkpoint_state(saved["tool_state"])
        return saved["result"]

    async def run(self, role: str, invoke: Callable[[dict], Awaitable[dict]], context: dict) -> dict:
        results = []
        repairs = []
        tool_repair_rounds = 0

        def retrieval_constraints():
            describe = getattr(self.tools, "retrieval_constraints", None)
            return describe(role) if describe is not None else None

        def tool_error(call: ToolCall, exc: HarnessError) -> dict:
            constraints = retrieval_constraints()
            if (call.name != "search_knowledge" or exc.code != "retrieval_policy_exceeded"
                    or constraints is None):
                raise exc
            if tool_repair_rounds >= 2:
                raise HarnessError("invalid_role_response") from exc
            return {"error": {
                "code": exc.code, "constraints": constraints,
                "instruction": "检索尚未执行。按实际条目数和查询长度限制修改参数，"
                               "或使用已读取资料完成审查；不得补造检索结果。",
            }}

        async def invoke_role(current):
            try:
                return await invoke(deepcopy(current))
            except HarnessError as exc:
                if exc.code != "invalid_role_response":
                    raise
                # 已收到但无法解码的结果可修复；未知完成/计费等错误绝不进入此分支。
                return {"protocol_error": "invalid_role_response"}

        async def replay_role(current):
            try:
                return await self.replay_model(role, deepcopy(current))
            except HarnessError as exc:
                if exc.code != "invalid_role_response":
                    raise
                return {"protocol_error": "invalid_role_response"}

        while self.journal is not None or self.model_turns < self.max_model_turns:
            self.before_call()
            current = {**deepcopy(context), "tool_results": deepcopy(results),
                       "tools": tool_schemas(retrieval_constraints())}
            if repairs:
                current["protocol_feedback"] = {
                    "instruction": "上次响应不符合完整响应结构。返回完整response_schema对象，"
                                   "或完整tool_calls对象；不要仅返回字段、断言片段或思考。",
                    "repairs": deepcopy(repairs),
                }
            if self.journal is not None and hasattr(self.journal, "model_context"):
                current = self.journal.model_context(role, current)
            if self.journal is None:
                self.model_turns += 1
                response = await invoke_role(current)
            else:
                response = await self.journal.invoke(
                    "model", {"role": role, "context": current},
                    lambda: invoke_role(current), limit=self.max_model_turns,
                    replay_operation=(
                        (lambda: replay_role(current))
                        if self.replay_model is not None else None
                    ),
                )
                self.model_turns = self.journal.attempts("model")
            if not isinstance(response, dict):
                raise HarnessError("invalid_role_response")
            try:
                size = len(json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            except (TypeError, ValueError, UnicodeError) as exc:
                raise HarnessError("invalid_role_response") from exc
            if size > 256 * 1024:
                raise HarnessError("role_response_too_large")
            # 也处理已提交的历史响应，恢复时不必再次请求模型。
            try:
                response = normalize_tool_response(response)
                if "tool_calls" not in response:
                    if response == {"protocol_error": "invalid_role_response"}:
                        raise ValueError("响应未能解析为完整JSON对象。")
                    if "response_schema" in context:
                        model = {"generator": CandidateReport, "reviewer": ReviewResult}[role]
                        model.model_validate(response)
                    return response
                turn = ToolTurn.model_validate(response)
            except (ValidationError, ValueError) as exc:
                if len(repairs) >= self.max_protocol_repairs:
                    raise HarnessError("invalid_role_response") from exc
                errors = exc.errors(include_input=False, include_url=False) if isinstance(
                    exc, ValidationError,
                ) else [{"type": "invalid_json", "loc": [], "msg": str(exc)}]
                # JSONB会重排对象键；反馈排序固定，崩溃重放才能命中原请求摘要。
                errors.sort(key=lambda item: json.dumps(
                    [list(item["loc"]), item["type"]], ensure_ascii=False, separators=(",", ":"),
                ))
                repairs.append({
                    "response_digest": canonical_digest(response),
                    "errors": [{
                        "type": item["type"],
                        "path": [part if type(part) is int else str(part)[:80]
                                 for part in item["loc"][:10]],
                        "message": item["msg"][:200],
                    } for item in errors[:16]],
                })
                continue
            denied_in_turn = False
            for call in turn.tool_calls:
                self.before_call()
                if self.journal is not None:
                    async def execute_tool():
                        result = await asyncio.to_thread(
                            self.tools.execute, role, call.name, call.arguments,
                        )
                        return {"result": result, "tool_state": self.tools.checkpoint_state()}

                    try:
                        saved = await self.journal.invoke(
                            "tool", {"role": role, "context_digest": canonical_digest(current),
                                     "call_id": call.call_id, "name": call.name,
                                     "arguments": deepcopy(call.arguments)},
                            execute_tool, limit=self.max_tool_calls,
                        )
                    except HarnessError as exc:
                        error_result = tool_error(call, exc)
                        self.tool_calls = self.journal.attempts("tool")
                        results.append({"call_id": call.call_id, "name": call.name,
                                        "result": error_result})
                        denied_in_turn = True
                        continue
                    self.tools.restore_checkpoint_state(saved["tool_state"])
                    self.tool_calls = self.journal.attempts("tool")
                    results.append({
                        "call_id": call.call_id, "name": call.name, "result": saved["result"],
                    })
                    continue
                if self.tool_calls >= self.max_tool_calls:
                    raise HarnessError("tool_budget_exhausted")
                self.tool_calls += 1
                private_call = {
                    "tool_calls": self.tool_calls, "model_turns": self.model_turns,
                    "call_id": call.call_id, "name": call.name,
                    "arguments": deepcopy(call.arguments),
                }
                call_record = {
                    "id": str(uuid4()), "role": role,
                    "name": call.name if call.name in {
                        "list_evidence", "read_evidence", "search_knowledge", "read_knowledge",
                    } else "unknown",
                    "call_id_digest": canonical_digest(call.call_id),
                    "arguments_digest": canonical_digest(call.arguments),
                }
                self.checkpoint(
                    {**call_record, "status": "intent"},
                    private_call,
                )
                try:
                    result = await asyncio.to_thread(self.tools.execute, role, call.name, call.arguments)
                except HarnessError as exc:
                    self.checkpoint(
                        {**call_record, "status": "denied", "code": exc.code},
                        private_call,
                    )
                    results.append({"call_id": call.call_id, "name": call.name,
                                    "result": tool_error(call, exc)})
                    denied_in_turn = True
                    continue
                self.checkpoint(
                    {**call_record, "status": "completed", "result_digest": canonical_digest(result)},
                    {**private_call, "result": deepcopy(result)},
                )
                results.append({"call_id": call.call_id, "name": call.name, "result": result})
            if denied_in_turn:
                tool_repair_rounds += 1
        raise HarnessError("model_turn_budget_exhausted")


def role_context(snapshot: dict, *, inline_limit=32000) -> tuple[dict, set[str]]:
    source_ids = {
        source for item in snapshot["fact_obligations"] for source in item["source_refs"]
    }
    if len(json.dumps(snapshot, ensure_ascii=False)) <= inline_limit:
        return deepcopy(snapshot), source_ids
    result = deepcopy(snapshot)
    result["accident_data"] = {"available_via": "read_evidence"}
    for record in result["supplemental_records"]:
        record.pop("text", None)
    result["content_mode"] = "catalogue"
    return result, set()
