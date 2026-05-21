import json
import logging
import time
from typing import Any

import httpx

from app.core.json_parser import extract_json_from_text
from app.core.exceptions import ModelTimeoutError, ProviderError
from app.core.settings import ReportModelSettings
from app.providers.llm.base import BaseLLMProvider, LLMGenerateResult, LLMToolCall
from app.providers.llm.lmstudio_compat import (
    build_lmstudio_models_urls,
    build_models_url,
    probe_lmstudio_model,
    resolve_lmstudio_compatibility,
)
from app.providers.lmstudio_residency import LMStudioResidencyManager, LMStudioResidencySpec

logger = logging.getLogger(__name__)


class OpenAIReportProvider(BaseLLMProvider):
    def __init__(
        self,
        config: ReportModelSettings,
        endpoint_api_keys: dict[str, str],
        lmstudio_host_allowlist: list[str] | None = None,
        selected_endpoint_label: str | None = None,
        switchable_labels: list[str] | None = None,
        lmstudio_residency_manager: LMStudioResidencyManager | None = None,
        lmstudio_residency_companions: list[LMStudioResidencySpec] | None = None,
    ):
        self.config = config
        self.endpoint_api_keys = endpoint_api_keys
        self.selected_endpoint_label = selected_endpoint_label
        self.switchable_labels = switchable_labels or []
        self._lmstudio_residency_manager = lmstudio_residency_manager
        self._lmstudio_residency_companions = list(lmstudio_residency_companions or [])
        self._clients = {
            endpoint.name: httpx.Client(timeout=endpoint.timeout_seconds)
            for endpoint in config.endpoints
        }
        self._lmstudio_host_allowlist = lmstudio_host_allowlist or []
        self._lmstudio_by_endpoint = {
            endpoint.name: resolve_lmstudio_compatibility(
                base_url_or_endpoint=endpoint.url,
                provider_name=config.provider,
                host_allowlist=self._lmstudio_host_allowlist,
                ttl_seconds=endpoint.lmstudio_ttl_seconds,
            )
            for endpoint in config.endpoints
        }
        self.last_used_endpoint_name: str | None = None
        self.last_used_endpoint_priority: int | None = None
        self.last_used_endpoint_url: str | None = None
        self.last_used_model: str | None = None
        self.last_finish_reason: str | None = None
        self.last_usage: dict[str, Any] | None = None
        self.last_reasoning_observed: bool = False
        self.last_reasoning_content_length: int = 0

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        result = self.generate_with_tools(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            tools=[],
        )
        return result.content

    @property
    def supports_tool_calling(self) -> bool:
        return True

    def generate_with_tools(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, Any]],
    ) -> LLMGenerateResult:
        errors: list[str] = []
        timeout_errors: list[str] = []
        truncation_errors: list[str] = []
        self.last_used_endpoint_name = None
        self.last_used_endpoint_priority = None
        self.last_used_endpoint_url = None
        self.last_used_model = None
        self.last_finish_reason = None
        self.last_usage = None
        self.last_reasoning_observed = False
        self.last_reasoning_content_length = 0

        for endpoint in self.config.iter_endpoints_by_priority():
            model_name = endpoint.model or self.config.model
            self._preflight_lmstudio_model_if_needed(endpoint=endpoint, model_name=model_name)
            self._ensure_lmstudio_residency(endpoint=endpoint, model_name=model_name)
            payload = self._build_payload(
                endpoint=endpoint,
                model_name=model_name,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                tools=tools,
            )
            headers = {
                "Authorization": f"Bearer {self.endpoint_api_keys[endpoint.name]}",
                "Content-Type": "application/json",
            }
            client = self._clients[endpoint.name]

            for attempt in range(1, self.config.retry.max_attempts_per_endpoint + 1):
                try:
                    resp = client.post(endpoint.url, headers=headers, json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    result = self._parse_response(data)
                    self._raise_for_truncated_response(
                        endpoint_name=endpoint.name,
                        model_name=model_name,
                        result=result,
                    )
                    self.last_used_endpoint_name = endpoint.name
                    self.last_used_endpoint_priority = endpoint.priority
                    self.last_used_endpoint_url = endpoint.url
                    self.last_used_model = model_name
                    self.last_finish_reason = str(result.response_metadata.get("finish_reason") or "").strip() or None
                    usage = result.response_metadata.get("usage")
                    self.last_usage = usage if isinstance(usage, dict) else None
                    self.last_reasoning_content_length = len(result.reasoning_content.strip())
                    self.last_reasoning_observed = self._has_reasoning_output(result)
                    return result
                except ProviderError as exc:
                    if exc.code == "REPORT_MODEL_OUTPUT_TRUNCATED":
                        error_message = (
                            f"端点 {endpoint.name}（模型 {model_name}）输出被长度上限截断: {exc}"
                        )
                        truncation_errors.append(error_message)
                        errors.append(error_message)
                        logger.warning(error_message)
                        break
                    error_message = (
                        f"端点 {endpoint.name}（模型 {model_name}）第 {attempt}/"
                        f"{self.config.retry.max_attempts_per_endpoint} 次调用失败: {exc}"
                    )
                    errors.append(error_message)
                    logger.warning(error_message)
                    if attempt < self.config.retry.max_attempts_per_endpoint:
                        time.sleep(self.config.retry.backoff_seconds * attempt)
                except httpx.TimeoutException as exc:
                    error_message = (
                        f"端点 {endpoint.name}（模型 {model_name}）第 {attempt}/"
                        f"{self.config.retry.max_attempts_per_endpoint} 次调用超时: {exc}"
                    )
                    timeout_errors.append(error_message)
                    errors.append(error_message)
                    logger.warning(error_message)
                    if attempt < self.config.retry.max_attempts_per_endpoint:
                        time.sleep(self.config.retry.backoff_seconds * attempt)
                except Exception as exc:  # noqa: BLE001
                    error_message = (
                        f"端点 {endpoint.name}（模型 {model_name}）第 {attempt}/"
                        f"{self.config.retry.max_attempts_per_endpoint} 次调用失败: {exc}"
                    )
                    errors.append(error_message)
                    logger.warning(error_message)

                    if attempt < self.config.retry.max_attempts_per_endpoint:
                        time.sleep(self.config.retry.backoff_seconds * attempt)

            if len(self.config.endpoints) > 1:
                logger.warning(
                    "报告模型端点 %s（模型 %s）不可用，准备切换到下一个端点。",
                    endpoint.name,
                    model_name,
                )

        if len(self.config.endpoints) == 1 and self.switchable_labels:
            if truncation_errors and len(truncation_errors) == len(errors):
                raise ProviderError(
                    "当前报告模型输出被长度上限截断。",
                    code="REPORT_MODEL_OUTPUT_TRUNCATED",
                    public_message="当前报告模型输出被长度上限截断，请切换其他报告模型或缩短输入后重试。",
                    details={
                        "errors": truncation_errors,
                        "selected_label": self.selected_endpoint_label,
                        "selected_endpoint_name": self.config.endpoints[0].name,
                        "switchable_labels": self.switchable_labels,
                        "failure_mode": "truncated",
                    },
                )
            failure_mode = "timeout" if timeout_errors and len(timeout_errors) == len(errors) else "unavailable"
            raise ProviderError(
                "当前报告模型端点不可用。",
                code="REPORT_MODEL_ENDPOINT_UNAVAILABLE",
                public_message="当前报告模型端点暂时不可用，请切换其他报告模型后重试。",
                details={
                    "errors": timeout_errors if failure_mode == "timeout" else errors,
                    "selected_label": self.selected_endpoint_label,
                    "selected_endpoint_name": self.config.endpoints[0].name,
                    "switchable_labels": self.switchable_labels,
                    "failure_mode": failure_mode,
                },
            )

        if truncation_errors and len(truncation_errors) == len(errors):
            raise ProviderError(
                "报告模型输出被长度上限截断，请缩短输入或切换模型后重试。",
                code="REPORT_MODEL_OUTPUT_TRUNCATED",
                details={"errors": truncation_errors},
            )
        if timeout_errors and len(timeout_errors) == len(errors):
            raise ModelTimeoutError(
                "报告模型响应超时，请稍后重试。",
                details={"errors": timeout_errors},
            )
        raise ProviderError(
            "报告模型暂时不可用，请稍后重试。",
            details={"errors": errors},
        )

    def health_check(self) -> bool:
        for endpoint in self.config.iter_endpoints_by_priority():
            headers = {"Authorization": f"Bearer {self.endpoint_api_keys[endpoint.name]}"}
            for url in self._health_probe_urls(endpoint):
                try:
                    resp = self._clients[endpoint.name].get(url, headers=headers)
                    resp.raise_for_status()
                    return True
                except Exception:  # noqa: BLE001
                    continue
        return False

    def close(self) -> None:
        for client in self._clients.values():
            client.close()

    def _build_payload(
        self,
        endpoint,
        model_name: str,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if endpoint.verbosity:
            payload["verbosity"] = endpoint.verbosity
        if endpoint.reasoning is not None:
            payload["reasoning"] = endpoint.reasoning.model_dump(exclude_none=True)
        elif endpoint.reasoning_effort:
            payload["reasoning_effort"] = endpoint.reasoning_effort
        if endpoint.extra_body:
            payload.update(endpoint.extra_body)
        lmstudio = self._lmstudio_by_endpoint[endpoint.name]
        if lmstudio.enabled and lmstudio.ttl_seconds is not None:
            payload["ttl"] = lmstudio.ttl_seconds
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = False
        return payload

    def _parse_response(self, data: dict[str, Any]) -> LLMGenerateResult:
        choices = data.get("choices") or []
        if not choices:
            raise ProviderError("OpenAI 返回为空。")

        choice = choices[0]
        message = choice.get("message") or {}
        reasoning_content = self._extract_content(message.get("reasoning_content"))
        return LLMGenerateResult(
            content=self._extract_content(message.get("content")),
            tool_calls=self._extract_tool_calls(message),
            reasoning_content=reasoning_content,
            response_metadata={
                "finish_reason": choice.get("finish_reason"),
                "usage": data.get("usage"),
                "response_id": data.get("id"),
                "response_model": data.get("model"),
                "reasoning_content_length": len(reasoning_content),
            },
        )

    def _extract_tool_calls(self, message: dict[str, Any]) -> list[LLMToolCall]:
        tool_calls: list[LLMToolCall] = []
        raw_calls = list(message.get("tool_calls") or [])

        legacy_call = message.get("function_call")
        if legacy_call and not raw_calls:
            raw_calls.append({"id": None, "type": "function", "function": legacy_call})

        for item in raw_calls:
            function = item.get("function") or {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue

            arguments_raw = function.get("arguments")
            arguments = self._parse_tool_arguments(arguments_raw)
            tool_calls.append(
                LLMToolCall(
                    name=name,
                    arguments=arguments,
                    call_id=item.get("id"),
                    raw_arguments=arguments_raw if isinstance(arguments_raw, str) else json.dumps(arguments, ensure_ascii=False),
                )
            )
        return tool_calls

    @staticmethod
    def _extract_content(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n".join(parts).strip()
        return ""

    @staticmethod
    def _parse_tool_arguments(raw_arguments: Any) -> dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if not isinstance(raw_arguments, str):
            raise ProviderError("工具调用参数不是合法的 JSON 对象。")
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError:
            parsed = extract_json_from_text(raw_arguments)
        if not isinstance(parsed, dict):
            raise ProviderError("工具调用参数不是合法的 JSON 对象。")
        return parsed

    def _raise_for_truncated_response(
        self,
        endpoint_name: str,
        model_name: str,
        result: LLMGenerateResult,
    ) -> None:
        finish_reason = str(result.response_metadata.get("finish_reason") or "").strip().lower()
        if finish_reason not in {"length", "max_tokens", "max_output_tokens"}:
            return
        raise ProviderError(
            "报告模型输出被长度上限截断。",
            code="REPORT_MODEL_OUTPUT_TRUNCATED",
            details={
                "endpoint_name": endpoint_name,
                "model": model_name,
                "finish_reason": finish_reason,
                "usage": result.response_metadata.get("usage"),
            },
        )

    @staticmethod
    def _has_reasoning_output(result: LLMGenerateResult) -> bool:
        if result.reasoning_content.strip():
            return True
        usage = result.response_metadata.get("usage")
        if not isinstance(usage, dict):
            return False
        completion_details = usage.get("completion_tokens_details")
        if not isinstance(completion_details, dict):
            return False
        reasoning_tokens = completion_details.get("reasoning_tokens")
        if isinstance(reasoning_tokens, (int, float)):
            return reasoning_tokens > 0
        return False

    def _preflight_lmstudio_model_if_needed(self, endpoint, model_name: str) -> None:  # noqa: ANN001
        lmstudio = self._lmstudio_by_endpoint[endpoint.name]
        if not lmstudio.enabled:
            return

        probe = probe_lmstudio_model(
            client=self._clients[endpoint.name],
            headers=self._build_headers(endpoint.name),
            chat_completions_url=lmstudio.chat_completions_url,
            model_name=model_name,
        )
        if probe.models_endpoint_accessible and probe.model_exists is False:
            raise ProviderError(
                f"LM Studio 未找到报告模型 `{model_name}`。请改用 `/v1/models` 返回的实际模型 id。",
                details={
                    "provider": "lmstudio",
                    "endpoint": endpoint.name,
                    "model": model_name,
                },
            )
        if not probe.models_endpoint_accessible:
            logger.warning(
                "报告模型 LM Studio 模型列表暂不可用，继续直接请求 chat/completions: endpoint=%s, model=%s, errors=%s",
                endpoint.name,
                model_name,
                probe.errors,
            )
            return

        logger.info(
            "报告模型已命中 LM Studio 兼容模式: endpoint=%s, host=%s, detected_by=%s, ttl=%s, loaded=%s",
            endpoint.name,
            lmstudio.host,
            lmstudio.detected_by,
            lmstudio.ttl_seconds,
            probe.loaded,
        )

    def _health_probe_urls(self, endpoint) -> list[str]:  # noqa: ANN001
        lmstudio = self._lmstudio_by_endpoint[endpoint.name]
        if lmstudio.enabled:
            return build_lmstudio_models_urls(lmstudio.chat_completions_url)
        return [build_models_url(endpoint.url)]

    def _ensure_lmstudio_residency(self, endpoint, model_name: str) -> None:  # noqa: ANN001
        lmstudio = self._lmstudio_by_endpoint[endpoint.name]
        if self._lmstudio_residency_manager is None or not lmstudio.enabled:
            return
        self._lmstudio_residency_manager.ensure_residency(
            primary_spec=LMStudioResidencySpec(
                model=model_name,
                base_url_or_endpoint=endpoint.url,
                api_key=self.endpoint_api_keys.get(endpoint.name, ""),
                provider_name=self.config.provider,
                ttl_seconds=lmstudio.ttl_seconds,
            ),
            companion_specs=self._lmstudio_residency_companions,
        )

    def _build_headers(self, endpoint_name: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.endpoint_api_keys[endpoint_name]}",
            "Content-Type": "application/json",
        }
