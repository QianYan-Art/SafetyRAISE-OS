from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pydantic import Field, model_validator

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.schemas.base import StrictModel


def tool_schemas() -> list[dict]:
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
    return [{"name": name, "parameters": {
        "type": "object", "properties": properties, "required": required,
        "additionalProperties": False,
    }} for name, properties, required in definitions]


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


class RoleLoop:
    """有界的角色/工具交互；逻辑轮次计数不是物理请求计费账本。"""

    def __init__(self, tools, checkpoint: Callable[[dict, dict], None], *,
                 before_call: Callable[[], None], max_model_turns=24, max_tool_calls=24,
                 journal=None, replay_model=None):
        self.tools, self.checkpoint, self.before_call = tools, checkpoint, before_call
        self.journal = journal
        self.replay_model = replay_model
        self.max_model_turns, self.max_tool_calls = max_model_turns, max_tool_calls
        self.model_turns = 0
        self.tool_calls = 0

    async def run(self, role: str, invoke: Callable[[dict], Awaitable[dict]], context: dict) -> dict:
        results = []
        while self.journal is not None or self.model_turns < self.max_model_turns:
            self.before_call()
            current = {**deepcopy(context), "tool_results": deepcopy(results),
                       "tools": tool_schemas()}
            if self.journal is None:
                self.model_turns += 1
                response = await invoke(current)
            else:
                response = await self.journal.invoke(
                    "model", {"role": role, "context": current},
                    lambda: invoke(deepcopy(current)), limit=self.max_model_turns,
                    replay_operation=(
                        (lambda: self.replay_model(role, deepcopy(current)))
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
            if "tool_calls" not in response:
                return response
            turn = ToolTurn.model_validate(response)
            for call in turn.tool_calls:
                self.before_call()
                if self.journal is not None:
                    async def execute_tool():
                        result = await asyncio.to_thread(
                            self.tools.execute, role, call.name, call.arguments,
                        )
                        return {"result": result, "tool_state": self.tools.checkpoint_state()}

                    saved = await self.journal.invoke(
                        "tool", {"role": role, "context_digest": canonical_digest(current),
                                 "call_id": call.call_id, "name": call.name,
                                 "arguments": deepcopy(call.arguments)},
                        execute_tool, limit=self.max_tool_calls,
                    )
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
                    raise
                self.checkpoint(
                    {**call_record, "status": "completed", "result_digest": canonical_digest(result)},
                    {**private_call, "result": deepcopy(result)},
                )
                results.append({"call_id": call.call_id, "name": call.name, "result": result})
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
