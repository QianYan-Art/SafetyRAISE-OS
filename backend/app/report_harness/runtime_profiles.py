from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json

from app.core.exceptions import InputValidationError
from app.core.json_parser import extract_json_from_text
from app.core.model_output import sanitize_model_text
from app.core.model_requests import omit_explicit_token_limits
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.transport import HTTPAttemptClient, RequestBound
from app.schemas.report_run import BudgetPolicy


_THOUGHT_FIELDS = frozenset({"think", "thinking", "reasoning", "reasoning_content", "reasoning_details"})
_STRICT_JSON_ROLES = frozenset({"generator", "reviewer"})


def _redact_thoughts(value):
    if isinstance(value, dict):
        return {key: _redact_thoughts(item) for key, item in value.items() if key not in _THOUGHT_FIELDS}
    if isinstance(value, list):
        return [_redact_thoughts(item) for item in value]
    if isinstance(value, str):
        return sanitize_model_text(value)
    return value


def _parse_strict_json_object(content: str) -> dict:
    """生成与审查只接受完整 JSON 对象，不从外层失败文本提取内层片段。"""
    normalized = content.strip()
    if not normalized:
        raise InputValidationError("生成或审查响应为空，无法解码 JSON 对象。")

    if normalized.startswith("```json"):
        if not normalized.endswith("```"):
            raise InputValidationError("生成或审查响应的 JSON 围栏不完整。")
        fenced = normalized[len("```json"):-3]
        if fenced.startswith("\r\n"):
            fenced = fenced[2:]
        elif fenced.startswith("\n"):
            fenced = fenced[1:]
        else:
            raise InputValidationError("生成或审查响应不是完整 JSON 围栏。")
        normalized = fenced.strip()

    try:
        result = json.loads(normalized)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InputValidationError("生成或审查响应无法解码为完整 JSON 对象。") from exc
    if not isinstance(result, dict):
        raise InputValidationError("生成或审查响应不是 JSON 对象。")
    return result


@dataclass(frozen=True)
class ModelCapacity:
    """供应商容量用于请求前预留，不转换成模型请求截断参数。"""

    model: str
    context_tokens: int
    output_tokens: int
    proof_digest: str
    effort: str | None = None

    def __post_init__(self):
        if (not self.model.strip() or type(self.context_tokens) is not int
                or self.context_tokens <= 0 or type(self.output_tokens) is not int
                or self.output_tokens < 0):
            raise ValueError("模型容量证明无效。")
        self.bound().validate(max(1, self.output_tokens))

    def bound(self) -> RequestBound:
        return RequestBound(
            self.context_tokens + self.output_tokens, self.output_tokens, self.proof_digest,
        )


def capacity_budget(capacities: dict[str, ModelCapacity], configured=None) -> BudgetPolicy:
    """未指定时按物理次数和容量生成记账预算；显式配置不足则拒绝，不静默扩额。"""
    maximum_output = max(item.output_tokens for item in capacities.values())
    first_round = sum(item.bound().total_tokens for item in capacities.values())
    maximum_request = max(item.bound().total_tokens for item in capacities.values())
    if configured is None:
        defaults = BudgetPolicy()
        configured = defaults.model_copy(update={
            "max_output_tokens_per_request": maximum_output,
            "max_total_tokens": defaults.max_physical_requests * maximum_request,
        })
    minimum_total = configured.max_physical_requests * maximum_request
    if (configured.max_output_tokens_per_request < maximum_output
            or configured.max_total_tokens < minimum_total or configured.max_physical_requests < 4):
        raise HarnessError("business_token_budget_insufficient", 503, {
            "minimum_first_round_tokens": first_round,
            "minimum_total_reservation": minimum_total,
            "minimum_output_reservation": maximum_output,
        })
    return configured


def capacity_from_metadata(metadata: dict, *, model: str, effort: str | None,
                           embedding: bool = False) -> ModelCapacity:
    if metadata.get("id") != model:
        raise HarnessError("model_metadata_mismatch")
    context = metadata.get("context_length")
    output = 0 if embedding else (metadata.get("top_provider") or {}).get("max_completion_tokens")
    if (type(context) is not int or context <= 0 or type(output) is not int
            or output < (0 if embedding else 1)):
        raise HarnessError("model_capacity_unverified")
    if effort is not None:
        efforts = (metadata.get("reasoning") or {}).get("supported_efforts")
        levels = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
        if (not isinstance(efforts, list) or not efforts
                or any(item not in levels for item in efforts)
                or effort != max(efforts, key=levels.index)):
            raise HarnessError("model_effort_not_highest")
    proof = {"model": model, "context_tokens": context, "output_tokens": output,
             "effort": effort, "source": deepcopy(metadata)}
    return ModelCapacity(model, context, output, canonical_digest(proof), effort)


def capacity_from_expert_metadata(metadata: dict, *, model: str) -> ModelCapacity:
    if metadata.get("id", metadata.get("key")) != model:
        raise HarnessError("model_metadata_mismatch")
    context = metadata.get("max_context_length")
    if type(context) is not int or context <= 0:
        raise HarnessError("model_capacity_unverified")
    return ModelCapacity(model, context, context, canonical_digest({
        "source": deepcopy(metadata), "policy": "专家上下文容量保守预留，不发送输出限制",
    }))


def capacity_from_local_embedding_metadata(metadata: dict, *, model: str) -> ModelCapacity:
    if metadata.get("id", metadata.get("key")) != model:
        raise HarnessError("model_metadata_mismatch")
    context = metadata.get("max_context_length")
    if metadata.get("type") not in {"embedding", "embeddings"} or type(context) is not int or context <= 0:
        raise HarnessError("model_capacity_unverified")
    return ModelCapacity(model, context, 0, canonical_digest({
        "source": deepcopy(metadata), "policy": "既有自有嵌入服务容量，无模型输出",
    }))


def price_upper_cny(metadata: dict, capacity: ModelCapacity, *, usd_to_cny: Decimal) -> Decimal:
    """仅接受已核验的文字/嵌入计费项；新增收费类型必须先补预算证明。"""
    if metadata.get("id") != capacity.model or not usd_to_cny.is_finite() or usd_to_cny <= 0:
        raise HarnessError("pricing_unverified")
    prices = metadata.get("pricing")
    if not isinstance(prices, dict) or not {"prompt", "completion"} <= prices.keys():
        raise HarnessError("pricing_unverified")
    parsed = {}
    try:
        for name, raw in prices.items():
            value = Decimal(str(raw))
            if not value.is_finite() or value < 0:
                raise ValueError("价格无效。")
            if name not in {"prompt", "completion", "input_cache_read"} and value != 0:
                raise ValueError("存在未核验计费项。")
            parsed[name] = value
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HarnessError("pricing_unverified") from exc
    return usd_to_cny * (
        capacity.context_tokens * (parsed["prompt"] + parsed.get("input_cache_read", Decimal(0)))
        + capacity.output_tokens * parsed["completion"]
    )


def openrouter_price_filter(metadata: dict, capacity: ModelCapacity) -> dict:
    """供应商筛选单价与本地预留使用同一份报价，不限制模型输出长度。"""
    price_upper_cny(metadata, capacity, usd_to_cny=Decimal(1))
    prices = metadata["pricing"]
    return {
        "allow_fallbacks": False, "require_parameters": True, "data_collection": "deny",
        "max_price": {
            "prompt": str(Decimal(str(prices["prompt"])) * 1_000_000),
            "completion": str(Decimal(str(prices["completion"])) * 1_000_000),
            "request": "0", "image": "0",
        },
    }


def _validate_provider_filter(provider):
    if (not isinstance(provider, dict)
            or set(provider) != {"allow_fallbacks", "require_parameters", "data_collection", "max_price"}
            or provider["allow_fallbacks"] is not False
            or provider["require_parameters"] is not True
            or provider["data_collection"] != "deny"):
        raise ValueError("远端路由必须禁止回退并固定隐私与价格过滤。")
    prices = provider["max_price"]
    if not isinstance(prices, dict) or set(prices) != {"prompt", "completion", "request", "image"}:
        raise ValueError("远端价格过滤不完整。")
    try:
        parsed = {name: Decimal(str(value)) for name, value in prices.items()}
    except InvalidOperation as exc:
        raise ValueError("远端价格过滤无效。") from exc
    if (any(not value.is_finite() or value < 0 for value in parsed.values())
            or parsed["request"] != 0 or parsed["image"] != 0):
        raise ValueError("远端价格过滤包含未获准的收费类别。")


class ConfiguredAttemptClient(HTTPAttemptClient):
    """角色配置由服务端冻结，模型不得覆盖模型名、推理等级或请求策略。"""

    def __init__(self, endpoints, headers, capacities: dict[str, ModelCapacity],
                 request_options=None, role_timeouts=None):
        if set(endpoints) != set(capacities):
            raise ValueError("角色端点与容量证明不一致。")
        self.capacities = dict(capacities)
        self.role_timeouts = dict(role_timeouts or {})
        if set(self.role_timeouts) - set(capacities) or any(
            type(value) not in {int, float} or not 0 < value < float("inf")
            for value in self.role_timeouts.values()
        ):
            raise ValueError("角色超时必须是正的有限秒数。")
        self.request_options = deepcopy(request_options or {})
        if set(self.request_options) - set(capacities):
            raise ValueError("请求参数包含未登记角色。")
        for role, options in self.request_options.items():
            if not isinstance(options, dict) or set(options) - {"temperature", "verbosity", "provider"}:
                raise ValueError("请求参数没有纳入运行合同。")
            if role == "embedding" and set(options) - {"provider"}:
                raise ValueError("嵌入请求不接受生成参数。")
            if "provider" in options:
                _validate_provider_filter(options["provider"])
            temperature = options.get("temperature")
            if temperature is not None and (
                type(temperature) not in {int, float} or not 0 <= temperature <= 2
            ):
                raise ValueError("模型温度无效。")
            if "verbosity" in options and options["verbosity"] not in {"low", "medium", "high"}:
                raise ValueError("模型表达详略无效。")
        super().__init__(endpoints, headers)

    async def attempt(self, role, payload, timeout):
        capacity = self.capacities.get(role)
        if capacity is None or payload.get("model") != capacity.model:
            raise HarnessError("runtime_model_mismatch")
        allowed = {"model", "input"} if role == "embedding" else {
            "model", "messages", "response_format",
        }
        if set(payload) - allowed:
            raise HarnessError("runtime_payload_unapproved")
        outbound = deepcopy(payload)
        outbound.update(self.request_options.get(role, {}))
        if capacity.effort is not None:
            outbound["reasoning"] = {"effort": capacity.effort, "exclude": True}
        omit_explicit_token_limits(outbound)
        effective_timeout = min(timeout, self.role_timeouts.get(role, timeout))
        async with asyncio.timeout(effective_timeout):
            response = await super().attempt(role, outbound, effective_timeout)
        for field in _THOUGHT_FIELDS:
            response.pop(field, None)
        choices = response.get("choices")
        for choice in choices if isinstance(choices, list) else []:
            message = choice.get("message") if isinstance(choice, dict) else None
            if not isinstance(message, dict):
                continue
            for field in _THOUGHT_FIELDS:
                message.pop(field, None)
            if isinstance(message.get("content"), str):
                try:
                    parsed = (
                        _parse_strict_json_object(message["content"])
                        if role in _STRICT_JSON_ROLES
                        else extract_json_from_text(message["content"])
                    )
                    message["content"] = json.dumps(
                        _redact_thoughts(parsed), ensure_ascii=False,
                        separators=(",", ":"), allow_nan=False,
                    )
                except (InputValidationError, ValueError):
                    # 无有效协议 JSON 时保留 usage，由角色解码显式判失败，不留原始思考。
                    message["content"] = ""
        return response
