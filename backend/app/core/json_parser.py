import ast
import ctypes
import json
import re
from typing import Any

from app.core.exceptions import InputValidationError
from app.core.model_output import sanitize_model_text
from app.core.rust_accel import load_rust_token_accel


def extract_json_from_text(text: str) -> dict[str, Any]:
    content = sanitize_model_text(text)
    if not content:
        raise InputValidationError("模型返回为空，无法解析 JSON。")

    candidates = _extract_candidates(content)

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


def _extract_candidates(content: str) -> list[str]:
    rust_candidates = _extract_candidates_with_rust(content)
    if rust_candidates:
        return rust_candidates

    candidates: list[str] = []
    fence_matches = re.findall(r"```(?:json)?\s*(.*?)\s*```", content, re.DOTALL | re.IGNORECASE)
    for item in fence_matches:
        fenced = item.strip()
        if fenced and "{" in fenced and "}" in fenced:
            candidates.append(fenced)

    if content.startswith("{") and content.endswith("}"):
        candidates.append(content)

    candidates.extend(_extract_balanced_candidates(content))
    return candidates


def _extract_candidates_with_rust(content: str) -> list[str]:
    rust_accel = load_rust_token_accel()
    if rust_accel is None or not hasattr(rust_accel, "accel_extract_json_candidates"):
        return []

    try:
        raw_ptr = rust_accel.accel_extract_json_candidates(content.encode("utf-8"))
        if not raw_ptr:
            return []
        try:
            payload = ctypes.string_at(raw_ptr).decode("utf-8")
        finally:
            rust_accel.accel_free_string(raw_ptr)
        parsed = json.loads(payload)
    except Exception:  # noqa: BLE001
        return []

    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


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
    normalized = _normalize_json_like_text(candidate)
    repaired = re.sub(r",(\s*[}\]])", r"\1", normalized)
    variants = [normalized]
    if repaired != normalized:
        variants.append(repaired)

    balanced = _append_missing_closers(repaired)
    if balanced not in variants:
        variants.append(balanced)

    normalized_balanced = _append_missing_closers(normalized)
    if normalized_balanced not in variants:
        variants.append(normalized_balanced)
    return variants


def _extract_balanced_candidates(content: str) -> list[str]:
    candidates: list[str] = []
    starts = [index for index, char in enumerate(content) if char == "{"] or []
    for start in starts:
        end = _find_balanced_json_end(content, start)
        if end is None:
            candidates.append(content[start:])
            continue
        candidates.append(content[start : end + 1])
    return sorted(dict.fromkeys(candidates), key=len, reverse=True)


def _find_balanced_json_end(content: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False
    quote_char = '"'

    for index in range(start, len(content)):
        char = content[index]
        if in_string:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == quote_char:
                in_string = False
            continue

        if char in {'"', "'"}:
            in_string = True
            quote_char = char
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def _normalize_json_like_text(candidate: str) -> str:
    normalized = candidate.strip().replace("\ufeff", "")
    normalized = re.sub(r"^\s*json\s*", "", normalized, flags=re.IGNORECASE)
    replacements = {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "：": ":",
        "，": ",",
        "（": "(",
        "）": ")",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return normalized


def _append_missing_closers(candidate: str) -> str:
    open_braces = candidate.count("{")
    close_braces = candidate.count("}")
    open_brackets = candidate.count("[")
    close_brackets = candidate.count("]")
    repaired = candidate
    if open_brackets > close_brackets:
        repaired += "]" * (open_brackets - close_brackets)
    if open_braces > close_braces:
        repaired += "}" * (open_braces - close_braces)
    return repaired
