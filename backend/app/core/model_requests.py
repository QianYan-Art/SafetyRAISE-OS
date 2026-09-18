"""模型请求参数约束；内部费用预留不转换为模型输出截断参数。"""

from typing import Any


def omit_explicit_token_limits(payload: dict[str, Any]) -> None:
    for field in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        payload.pop(field, None)
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict) and (
        reasoning.get("effort") is not None or payload.get("reasoning_effort") is not None
    ):
        # extra_body可能引用原配置对象，不在清理请求时修改配置。
        payload["reasoning"] = {key: value for key, value in reasoning.items() if key != "max_tokens"}
