import ast
import json
import re
from typing import Any

from app.core.exceptions import InputValidationError
from app.core.model_output import sanitize_model_text


def extract_json_from_text(text: str) -> dict[str, Any]:
    content = sanitize_model_text(text)
    if not content:
        raise InputValidationError("模型返回为空，无法解析 JSON。")

    candidates: list[str] = []
    fence_matches = re.findall(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
    for item in fence_matches:
        fenced = item.strip()
        if fenced and "{" in fenced and "}" in fenced:
            candidates.append(fenced)

    if content.startswith("{") and content.endswith("}"):
        candidates.append(content)

    first = content.find("{")
    last = content.rfind("}")
    if first != -1 and last != -1 and last > first:
        candidates.append(content[first : last + 1])

    if not candidates:
        raise InputValidationError("未在模型输出中找到可解析 JSON。")

    seen: set[str] = set()
    last_error: InputValidationError | None = None
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        try:
            return _load_json(normalized)
        except InputValidationError as exc:
            last_error = exc

    if last_error:
        raise last_error
    raise InputValidationError("未在模型输出中找到可解析 JSON。")


def _load_json(candidate: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for variant in _build_json_variants(candidate):
        try:
            loaded = json.loads(variant)
        except json.JSONDecodeError as exc:
            last_error = exc
        else:
            if not isinstance(loaded, dict):
                raise InputValidationError("指导意见必须为 JSON 对象。")
            return loaded

        try:
            loaded = ast.literal_eval(variant)
        except (SyntaxError, ValueError) as exc:
            last_error = exc
            continue

        if not isinstance(loaded, dict):
            raise InputValidationError("指导意见必须为 JSON 对象。")
        return loaded

    raise InputValidationError(f"JSON 解析失败: {last_error}") from last_error


def _build_json_variants(candidate: str) -> list[str]:
    normalized = candidate.strip().replace("\ufeff", "")
    repaired = re.sub(r",(\s*[}\]])", r"\1", normalized)
    variants = [normalized]
    if repaired != normalized:
        variants.append(repaired)
    return variants
