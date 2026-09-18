"""隔离开发试跑的固定供应商协议；生产配置不导入本模块。"""

from copy import deepcopy
from decimal import Decimal
import httpx

from app.report_harness.errors import HarnessError
from app.report_harness.transport import HTTPAttemptClient


MODEL = "tencent/hy4-preview"
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
CONTEXT_LIMIT = 1048576
MODEL_OUTPUT_LIMIT = 64000
OUTPUT_LIMIT = 16384
TOKEN_BOUND = CONTEXT_LIMIT + MODEL_OUTPUT_LIMIT
USD_TO_CNY_UPPER = Decimal("10")
REQUEST_CNY_UPPER = Decimal("24")
GENERATION_SETTINGS = {"reasoning": {"effort": "low", "exclude": True}}


def validate_metadata(data: dict) -> dict:
    """按模型总容量预留，不用字符数猜token；变价或能力变化时停止。"""
    if data.get("id") != MODEL:
        raise ValueError("模型元数据不匹配。")
    choices = [item for item in data.get("endpoints", []) if item.get("tag") == "tencent/fp8"]
    if len(choices) != 1:
        raise ValueError("指定供应商端点不唯一或不可用。")
    item = choices[0]
    if (item.get("context_length") != CONTEXT_LIMIT
            or item.get("max_completion_tokens") != MODEL_OUTPUT_LIMIT):
        raise ValueError("模型容量变化，需要重新核验预算证明。")
    prices = item["pricing"]
    limits = {"prompt": Decimal("0.000001"), "completion": Decimal("0.000003"),
              "input_cache_read": Decimal("0.000001")}
    for key, value in prices.items():
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("无效价格。")
        if key in limits:
            if amount > limits[key]:
                raise ValueError("价格超过预注册上限。")
        elif amount != 0:
            raise ValueError("存在未经预留的附加计费项。")
    if not {"prompt", "completion"} <= prices.keys():
        raise ValueError("缺少必要价格。")
    if not {"max_tokens", "response_format", "reasoning"} <= set(item["supported_parameters"]):
        raise ValueError("所需请求能力未登记。")
    maximum = (CONTEXT_LIMIT * (limits["prompt"] + limits["input_cache_read"])
               + MODEL_OUTPUT_LIMIT * limits["completion"]) * USD_TO_CNY_UPPER
    if maximum > REQUEST_CNY_UPPER:
        raise ValueError("请求预留不足。")
    return deepcopy(item)


class DevelopmentClient(HTTPAttemptClient):
    """固定模型、价格和供应商；不向远端发送参照答案或隐藏推理请求。"""

    def __init__(self, key: str):
        if not key or key.strip() != key:
            raise ValueError("缺少有效API密钥。")
        roles = ("generator", "reviewer")
        super().__init__(
            {role: ENDPOINT for role in roles},
            {role: {"Authorization": f"Bearer {key}"} for role in roles},
        )
        self.last_http_status = None
        self.last_error_type = None

    async def attempt(self, role, payload, timeout):
        if (role not in self.registered_roles or payload.get("model") != MODEL
                or payload.get("max_tokens") != OUTPUT_LIMIT):
            raise HarnessError("development_profile_mismatch")
        if set(payload) - {"model", "messages", "max_tokens", "response_format"}:
            raise HarnessError("development_payload_unapproved")
        outbound = deepcopy(payload)
        outbound.update({
            "provider": {
                "only": ["tencent/fp8"], "allow_fallbacks": False,
                "require_parameters": True, "data_collection": "deny",
                "max_price": {"prompt": 1, "completion": 3, "request": 0, "image": 0},
            },
            **deepcopy(GENERATION_SETTINGS),
            "stream": False,
        })
        try:
            response = await super().attempt(role, outbound, timeout)
        except httpx.HTTPStatusError as exc:
            self.last_http_status = exc.response.status_code
            self.last_error_type = type(exc).__name__
            raise
        except Exception as exc:
            self.last_error_type = type(exc).__name__
            raise
        self.last_http_status = 200
        if response.get("model") != MODEL or response.get("provider") != "Tencent":
            raise HarnessError("development_response_profile_mismatch")
        # 不保存供应商意外返回的隐藏推理；只保留正文和可观测计费元数据。
        for choice in response.get("choices", []):
            message = choice.get("message", {})
            message.pop("reasoning", None)
            message.pop("reasoning_details", None)
        return response
