"""开发模型协议使用原句定位；核心候选契约仍使用精确字符区间。"""

from copy import deepcopy

from app.report_harness.errors import HarnessError
from app.report_harness.transport_roles import TransportRoles


def resolve_quotes(candidate: dict) -> dict:
    result = deepcopy(candidate)
    text = result.get("report_markdown")
    if not isinstance(text, str) or not isinstance(result.get("claims"), list):
        raise HarnessError("invalid_role_response")
    for claim in result["claims"]:
        if not isinstance(claim, dict) or "text_span" in claim:
            raise HarnessError("invalid_role_response")
        quote = claim.pop("quote", None)
        if not isinstance(quote, str) or not quote.strip():
            raise HarnessError("invalid_role_response")
        start = text.find(quote)
        if start < 0 or text.find(quote, start + 1) >= 0:
            raise HarnessError("invalid_role_response")
        claim["text_span"] = {"start": start, "end": start + len(quote)}
    return result


class QuotedTransportRoles(TransportRoles):
    def _payload(self, role, context):
        wire = deepcopy(context)
        if role == "generator":
            schema = wire["response_schema"]
            definition = schema["$defs"]["Claim"]
            definition["properties"].pop("text_span")
            definition["properties"]["quote"] = {
                "type": "string", "minLength": 1,
                "description": "report_markdown中仅出现一次的连续原文，完整复制，不要计算字符下标。",
            }
            definition["required"] = [
                "quote" if item == "text_span" else item
                for item in definition["required"]
            ]
            schema["$defs"].pop("TextSpan", None)
            wire["instructions"] += (
                "\n本次传输协议：claims用quote逐字引用正文中唯一出现的连续原句，"
                "不生成text_span，也不要计算字符位置；位置由程序精确计算。"
                "正文引用的内容仍必须有证据支持，不能因位置由程序计算而降低事实审查。"
            )
        return super()._payload(role, wire)

    @staticmethod
    def _decode(response):
        result = TransportRoles._decode(response)
        if "report_markdown" in result:
            return resolve_quotes(result)
        return result
