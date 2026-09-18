from __future__ import annotations

import json
import re
from typing import Any

from app.core.exceptions import InputValidationError


GUIDANCE_ACCIDENT_PLACEHOLDER = "{在这里粘贴结构化事故信息JSON}"
GUIDANCE_ACCIDENT_SECTION_PATTERN = re.compile(
    r"(<事故信息>\s*)(.*?)(\s*</事故信息>)",
    re.DOTALL,
)
REPORT_GUIDANCE_PLACEHOLDER = "{在这里粘贴指导意见JSON}"
REPORT_ACCIDENT_PLACEHOLDER = "{在这里粘贴结构化事故信息JSON}"
REPORT_ACCIDENT_ANCHOR_PLACEHOLDER = "{在这里粘贴事故信息关键锚点摘要}"
REPORT_INITIAL_SNIPPETS_PLACEHOLDER = "{在这里粘贴首轮知识库片段JSON}"
REPORT_ADDITIONAL_SNIPPETS_PLACEHOLDER = "{在这里粘贴模型追加检索获得的新知识库片段JSON}"
REPORT_AGENTIC_HISTORY_PLACEHOLDER = "{在这里粘贴模型追加检索历史摘要}"


def _strip_internal_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_internal_fields(item)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, list):
        return [_strip_internal_fields(item) for item in value]
    return value


def _build_accident_anchor_summary(accident_data: dict[str, Any]) -> str:
    preferred_fields = [
        ("事故标题", ["事故标题"]),
        ("事故类型", ["事故类型"]),
        ("事故形态", ["事故形态"]),
        ("事故发生时间", ["事故发生时间", "事故时间"]),
        ("地点（包括路名，路号）", ["地点（包括路名，路号）"]),
        ("路口路段类型", ["路口路段类型"]),
        ("主要违法行为", ["主要违法行为"]),
        ("事故认定原因", ["事故认定原因"]),
        ("车辆类型", ["车辆类型"]),
        ("伤害程度", ["伤害程度", "伤亡情况"]),
    ]
    summary: dict[str, Any] = {}
    for summary_key, candidate_keys in preferred_fields:
        for key in candidate_keys:
            value = accident_data.get(key)
            if isinstance(value, str) and value.strip():
                summary[summary_key] = value.strip()
                break

    if not summary:
        for key, value in accident_data.items():
            if not isinstance(value, str) or not value.strip():
                continue
            summary[key] = value.strip()
            if len(summary) >= 8:
                break

    if not summary:
        summary = {"提示": "事故信息中暂无可提取的关键锚点"}
    return json.dumps(summary, ensure_ascii=False, indent=2)


def _summarize_agentic_rounds(agentic_rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "round": item["round"],
            "query": item["query"],
            "reason": item["reason"],
            "requested_top_k": item["requested_top_k"],
            "returned_count": item["returned_count"],
            "snippet_ids": [snippet.get("id", "") for snippet in item["snippets"]],
        }
        for item in agentic_rounds
    ]


def render_guidance_prompt(template: str, accident_data: dict[str, Any]) -> str:
    """渲染原专家模板，保持原有占位替换规则。"""
    prompt_accident_data = _strip_internal_fields(accident_data)
    accident_json = json.dumps(prompt_accident_data, ensure_ascii=False, indent=2)

    if GUIDANCE_ACCIDENT_PLACEHOLDER in template:
        return template.replace(GUIDANCE_ACCIDENT_PLACEHOLDER, accident_json, 1)

    if GUIDANCE_ACCIDENT_SECTION_PATTERN.search(template):
        return GUIDANCE_ACCIDENT_SECTION_PATTERN.sub(
            lambda match: f"{match.group(1)}{accident_json}{match.group(3)}",
            template,
            count=1,
        )

    raise InputValidationError("指导意见提示词模板缺少事故信息粘贴位置。")


def render_report_prompt(
    template: str,
    accident_data: dict[str, Any],
    guidance: dict[str, Any],
    initial_snippets: list[dict[str, Any]],
    additional_snippets: list[dict[str, Any]],
    agentic_rounds: list[dict[str, Any]],
) -> str:
    """渲染原报告模板，保持每个占位符只替换首次匹配的语义。"""
    prompt_accident_data = _strip_internal_fields(accident_data)
    replacements = {
        REPORT_GUIDANCE_PLACEHOLDER: json.dumps(guidance, ensure_ascii=False, indent=2),
        REPORT_ACCIDENT_PLACEHOLDER: json.dumps(prompt_accident_data, ensure_ascii=False, indent=2),
        REPORT_ACCIDENT_ANCHOR_PLACEHOLDER: _build_accident_anchor_summary(prompt_accident_data),
        REPORT_INITIAL_SNIPPETS_PLACEHOLDER: json.dumps(initial_snippets, ensure_ascii=False, indent=2),
        REPORT_ADDITIONAL_SNIPPETS_PLACEHOLDER: json.dumps(
            additional_snippets,
            ensure_ascii=False,
            indent=2,
        ),
        REPORT_AGENTIC_HISTORY_PLACEHOLDER: json.dumps(
            _summarize_agentic_rounds(agentic_rounds),
            ensure_ascii=False,
            indent=2,
        ),
    }

    rendered = template
    for placeholder, value in replacements.items():
        if placeholder not in rendered:
            raise InputValidationError(f"分析报告提示词模板缺少占位内容: {placeholder}")
        rendered = rendered.replace(placeholder, value, 1)
    return rendered
