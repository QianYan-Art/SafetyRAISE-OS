import asyncio
import json
from copy import deepcopy

import pytest

from app.core.exceptions import InputValidationError
from app.report_harness.business_roles import BusinessTransportRoles
from app.report_harness.errors import HarnessError
from app.report_harness.quoted_roles import QuotedTransportRoles, resolve_quotes
from app.report_harness.transport_roles import RoleModel
from app.schemas.report_run import CandidateReport
from app.workflow.prompt_rendering import (
    GUIDANCE_ACCIDENT_PLACEHOLDER,
    REPORT_ACCIDENT_ANCHOR_PLACEHOLDER,
    REPORT_ACCIDENT_PLACEHOLDER,
    REPORT_ADDITIONAL_SNIPPETS_PLACEHOLDER,
    REPORT_AGENTIC_HISTORY_PLACEHOLDER,
    REPORT_GUIDANCE_PLACEHOLDER,
    REPORT_INITIAL_SNIPPETS_PLACEHOLDER,
    render_guidance_prompt,
    render_report_prompt,
)


class FakeTransport:
    output_limit = 64000

    def __init__(self, response):
        self.response = response
        self.request_calls = []
        self.replay_calls = []
        self.preflight_calls = 0

    def preflight_initial(self):
        self.preflight_calls += 1

    async def request(self, role, payload, *, output_limit_field=None):
        self.request_calls.append({
            "role": role,
            "payload": deepcopy(payload),
            "output_limit_field": output_limit_field,
        })
        return deepcopy(self.response)

    async def replay(self, role, payload, *, output_limit_field=None):
        self.replay_calls.append({
            "role": role,
            "payload": deepcopy(payload),
            "output_limit_field": output_limit_field,
        })
        return deepcopy(self.response)

    async def close(self):
        return None


def _response(content):
    return {
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": content},
        }],
    }


def _models():
    return {
        role: RoleModel(f"synthetic-{role}", output_limit_field="max_tokens")
        for role in ("expert", "generator", "reviewer")
    }


def _prompts():
    report = "|".join([
        REPORT_GUIDANCE_PLACEHOLDER,
        REPORT_ACCIDENT_PLACEHOLDER,
        REPORT_ACCIDENT_ANCHOR_PLACEHOLDER,
        REPORT_INITIAL_SNIPPETS_PLACEHOLDER,
        REPORT_ADDITIONAL_SNIPPETS_PLACEHOLDER,
        REPORT_AGENTIC_HISTORY_PLACEHOLDER,
    ])
    return {"expert": f"专家模板\n{GUIDANCE_ACCIDENT_PLACEHOLDER}", "report": report}


def _snapshot():
    return {
        "accident_data": {"事故标题": "中文事故", "_private": "不可发送"},
        "fact_obligations": [],
    }


def _roles(transport):
    return BusinessTransportRoles(
        transport,
        _models(),
        business_prompts=_prompts(),
    )


def test_legacy_rendering_preserves_first_match_and_internal_field_rules():
    accident = {"事故标题": "中文事故", "_private": "隐藏"}
    accident_json = json.dumps({"事故标题": "中文事故"}, ensure_ascii=False, indent=2)
    guidance_template = f"前{GUIDANCE_ACCIDENT_PLACEHOLDER}中{GUIDANCE_ACCIDENT_PLACEHOLDER}后"
    assert render_guidance_prompt(guidance_template, accident) == (
        f"前{accident_json}中{GUIDANCE_ACCIDENT_PLACEHOLDER}后"
    )

    report = _prompts()["report"] + REPORT_GUIDANCE_PLACEHOLDER
    rendered = render_report_prompt(report, accident, {"建议": "保留"}, [], [], [])
    assert REPORT_GUIDANCE_PLACEHOLDER in rendered
    assert rendered.count(REPORT_GUIDANCE_PLACEHOLDER) == 1
    assert "_private" not in rendered


def test_missing_prompt_location_is_rejected():
    with pytest.raises(InputValidationError):
        render_guidance_prompt("没有事故信息位置", {})
    with pytest.raises(InputValidationError):
        render_report_prompt("没有报告占位符", {}, {}, [], [], [])


def test_business_roles_require_expert_and_exact_prompt_keys():
    transport = FakeTransport(_response('{"guidance": {}}'))
    with pytest.raises(ValueError):
        BusinessTransportRoles(
            transport,
            {role: model for role, model in _models().items() if role != "expert"},
            business_prompts=_prompts(),
        )
    with pytest.raises(ValueError):
        BusinessTransportRoles(
            transport,
            _models(),
            business_prompts={"expert": "只有专家模板"},
        )


def test_expert_uses_original_user_prompt_cleans_think_and_calls_once():
    transport = FakeTransport(_response("思考过程\n```json\n{\"建议\": \"保留\"}\n```"))
    roles = _roles(transport)

    prepared = asyncio.run(roles.prepare(_snapshot()))

    assert prepared == {"guidance": {"建议": "保留"}, "knowledge": []}
    assert transport.preflight_calls == 1
    assert len(transport.request_calls) == 1
    call = transport.request_calls[0]
    assert call["role"] == "expert"
    assert call["output_limit_field"] is None
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & call["payload"].keys()
    assert call["payload"]["messages"][0]["role"] == "system"
    assert "专家模板" not in call["payload"]["messages"][0]["content"]
    assert "guidance" not in call["payload"]["messages"][0]["content"].lower()
    assert "tool_calls" not in call["payload"]["messages"][0]["content"]
    assert "需要工具时" not in call["payload"]["messages"][0]["content"]
    assert "专家模板" in call["payload"]["messages"][1]["content"]
    assert "不可发送" not in call["payload"]["messages"][1]["content"]


def test_replay_prepare_reuses_identical_expert_payload():
    response = _response("{\"建议\": \"已结算\"}")
    transport = FakeTransport(response)
    roles = _roles(transport)
    snapshot = _snapshot()

    asyncio.run(roles.prepare(snapshot))
    replayed = asyncio.run(roles.replay_prepare(snapshot))

    assert replayed == {"guidance": {"建议": "已结算"}, "knowledge": []}
    assert len(transport.request_calls) == 1
    assert len(transport.replay_calls) == 1
    assert transport.request_calls[0]["payload"] == transport.replay_calls[0]["payload"]
    assert transport.replay_calls[0]["output_limit_field"] is None


def test_generator_keeps_report_prompt_adds_tool_knowledge_and_quote_wire_contract():
    transport = FakeTransport(_response("{}"))
    roles = _roles(transport)
    context = {
        "instructions": "harness generator instructions",
        "response_schema": CandidateReport.model_json_schema(),
        "snapshot": _snapshot(),
        "prepared": {"guidance": {"建议": "专家意见"}},
        "initial_knowledge_snippets": [{"id": "initial-1", "text": "首轮知识"}],
        "tool_results": [{
            "name": "search_knowledge",
            "result": {"items": [{"id": "additional-1", "text": "追加知识"}]},
        }],
        "candidate_version": 1,
        "previous_candidate": None,
        "unresolved_issues": [],
        "review_feedback": None,
    }
    _, payload = roles._payload("generator", context)
    structured = json.loads(payload["messages"][2]["content"])
    report_prompt = payload["messages"][1]["content"]

    assert "追加知识" in report_prompt
    assert "专家意见" in report_prompt
    assert REPORT_GUIDANCE_PLACEHOLDER not in report_prompt
    assert "report_markdown必须承载原业务报告正文" in payload["messages"][0]["content"]
    claim = structured["response_schema"]["$defs"]["Claim"]
    assert "quote" in claim["required"]
    assert "text_span" not in claim["properties"]
    assert not {"max_tokens", "max_completion_tokens", "max_output_tokens"} & payload.keys()


def test_reviewer_payload_does_not_receive_expert_or_generator_history():
    transport = FakeTransport(_response("{}"))
    roles = _roles(transport)
    context = {
        "instructions": "harness reviewer instructions",
        "snapshot": _snapshot(),
        "snapshot_digest": "snapshot-digest",
        "candidate": {"report_markdown": "当前候选"},
        "candidate_digest": "candidate-digest",
        "unresolved_issues": [],
        "prepared": {"guidance": {"secret": "专家历史"}},
        "initial_knowledge_snippets": [{"id": "generator-history", "text": "生成历史"}],
        "previous_candidate": {"secret": "上一轮生成"},
        "review_feedback": {"secret": "生成反馈"},
        "tool_results": [],
    }
    _, payload = roles._payload("reviewer", context)
    wire = json.loads(payload["messages"][1]["content"])
    serialized = json.dumps(wire, ensure_ascii=False)

    assert all(field not in wire for field in (
        "prepared", "initial_knowledge_snippets", "previous_candidate", "review_feedback",
    ))
    assert all(secret not in serialized for secret in ("专家历史", "生成历史", "上一轮生成", "生成反馈"))
    assert "专家模板" not in json.dumps(payload, ensure_ascii=False)


def test_quote_resolution_rejects_missing_and_ambiguous_references():
    for text, quote in (("甲乙", "丙"), ("甲甲", "甲"), ("甲", "")):
        with pytest.raises(HarnessError):
            resolve_quotes({"report_markdown": text, "claims": [{"quote": quote}]})


def test_additional_knowledge_keeps_paginated_segments_with_page_state():
    result = BusinessTransportRoles._additional_knowledge(
        [
            {
                "name": "read_knowledge",
                "result": {
                    "items": [{
                        "id": "chunk-1", "text": "前半", "text_start": 0,
                        "text_end": 2, "text_length": 4, "text_complete": False,
                    }],
                    "next_cursor": "cursor-1", "truncated": True,
                },
            },
            {
                "name": "read_knowledge",
                "result": {
                    "items": [{
                        "id": "chunk-1", "text": "后半", "text_start": 2,
                        "text_end": 4, "text_length": 4, "text_complete": True,
                    }],
                    "next_cursor": None, "truncated": False,
                },
            },
        ],
        [{"id": "initial-1", "text": "首轮知识"}],
    )

    assert [item["id"] for item in result] == ["chunk-1", "chunk-1"]
    assert [item["text"] for item in result] == ["前半", "后半"]
    assert [item["read_page"] for item in result] == [1, 2]
    assert result[0]["text_complete"] is False
    assert result[0]["read_truncated"] is True
    assert result[0]["read_next_cursor"] == "cursor-1"
    assert result[1]["text_complete"] is True


def test_business_roles_use_core_quote_adapter_and_eval_compatibility():
    from evals.report_harness.quoted_roles import QuotedTransportRoles as EvalQuotedRoles

    assert issubclass(BusinessTransportRoles, QuotedTransportRoles)
    assert EvalQuotedRoles is QuotedTransportRoles
