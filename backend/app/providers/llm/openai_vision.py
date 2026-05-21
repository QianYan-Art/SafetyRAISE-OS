import base64
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from app.core.exceptions import ModelTimeoutError, ProviderError
from app.core.settings import ReportModelSettings
from app.providers.llm.lmstudio_compat import (
    build_lmstudio_models_urls,
    build_models_url,
    probe_lmstudio_model,
    resolve_lmstudio_compatibility,
)

logger = logging.getLogger(__name__)


class OpenAIVisionProvider:
    def __init__(
        self,
        config: ReportModelSettings,
        endpoint_api_keys: dict[str, str],
        lmstudio_host_allowlist: list[str] | None = None,
    ):
        self.config = config
        self.endpoint_api_keys = endpoint_api_keys
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

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        image_paths: list[Path],
        image_captions: Optional[list[str]] = None,
    ) -> str:
        if not image_paths:
            raise ProviderError("视觉模型调用缺少图片输入。")

        errors: list[str] = []
        timeout_errors: list[str] = []
        self.last_used_endpoint_name = None
        self.last_used_endpoint_priority = None
        self.last_used_endpoint_url = None
        self.last_used_model = None

        content = [{"type": "text", "text": user_prompt}]
        for index, image_path in enumerate(image_paths):
            if image_captions and index < len(image_captions):
                content.append({"type": "text", "text": image_captions[index]})
            content.append(self._build_image_payload(image_path))

        for endpoint in self.config.iter_endpoints_by_priority():
            model_name = endpoint.model or self.config.model
            self._preflight_lmstudio_model_if_needed(endpoint=endpoint, model_name=model_name)
            payload = self._build_payload(
                endpoint=endpoint,
                model_name=model_name,
                system_prompt=system_prompt,
                user_content=content,
            )
            headers = {
                "Authorization": f"Bearer {self.endpoint_api_keys[endpoint.name]}",
                "Content-Type": "application/json",
            }
            client = self._clients[endpoint.name]

            for attempt in range(1, self.config.retry.max_attempts_per_endpoint + 1):
                try:
                    response = client.post(endpoint.url, headers=headers, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    choices = data.get("choices") or []
                    if not choices:
                        raise ProviderError("视觉模型返回为空。")

                    self.last_used_endpoint_name = endpoint.name
                    self.last_used_endpoint_priority = endpoint.priority
                    self.last_used_endpoint_url = endpoint.url
                    self.last_used_model = model_name
                    return self._extract_content(choices[0].get("message", {}).get("content"))
                except httpx.TimeoutException as exc:
                    error_message = (
                        f"视觉端点 {endpoint.name}（模型 {model_name}）第 {attempt}/"
                        f"{self.config.retry.max_attempts_per_endpoint} 次调用超时: {exc}"
                    )
                    timeout_errors.append(error_message)
                    errors.append(error_message)
                    logger.warning(error_message)
                    if attempt < self.config.retry.max_attempts_per_endpoint:
                        time.sleep(self.config.retry.backoff_seconds * attempt)
                except Exception as exc:  # noqa: BLE001
                    error_message = (
                        f"视觉端点 {endpoint.name}（模型 {model_name}）第 {attempt}/"
                        f"{self.config.retry.max_attempts_per_endpoint} 次调用失败: {exc}"
                    )
                    errors.append(error_message)
                    logger.warning(error_message)
                    if attempt < self.config.retry.max_attempts_per_endpoint:
                        time.sleep(self.config.retry.backoff_seconds * attempt)

            logger.warning(
                "视觉模型端点 %s（模型 %s）不可用，准备切换到下一个端点。",
                endpoint.name,
                model_name,
            )

        if timeout_errors and len(timeout_errors) == len(errors):
            raise ModelTimeoutError(
                "视觉模型响应超时，请稍后重试。",
                details={"errors": timeout_errors},
            )
        raise ProviderError(
            "视觉模型暂时不可用，请稍后重试。",
            details={"errors": errors},
        )

    def health_check(self) -> bool:
        for endpoint in self.config.iter_endpoints_by_priority():
            headers = self._build_headers(endpoint.name)
            for url in self._health_probe_urls(endpoint):
                try:
                    response = self._clients[endpoint.name].get(url, headers=headers)
                    response.raise_for_status()
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
        user_content: list[dict[str, object]],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_name,
            "max_tokens": self.config.max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        temperature = endpoint.temperature if endpoint.temperature is not None else self.config.temperature
        if temperature is not None:
            payload["temperature"] = temperature
        if endpoint.verbosity:
            payload["verbosity"] = endpoint.verbosity
        if endpoint.reasoning is not None:
            payload["reasoning"] = endpoint.reasoning.model_dump(exclude_none=True)
        elif endpoint.reasoning_effort:
            payload["reasoning_effort"] = endpoint.reasoning_effort
        lmstudio = self._lmstudio_by_endpoint[endpoint.name]
        if lmstudio.enabled and lmstudio.ttl_seconds is not None:
            payload["ttl"] = lmstudio.ttl_seconds
        return payload

    def _build_image_payload(self, image_path: Path) -> dict[str, object]:
        if not image_path.exists():
            raise ProviderError(f"视觉模型图片不存在: {image_path}")

        mime_type, _ = mimetypes.guess_type(image_path.name)
        if not mime_type:
            mime_type = "image/jpeg"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
        }

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
                f"LM Studio 未找到视觉模型 `{model_name}`。请改用 `/v1/models` 返回的实际模型 id。",
                details={
                    "provider": "lmstudio",
                    "endpoint": endpoint.name,
                    "model": model_name,
                },
            )
        if not probe.models_endpoint_accessible:
            logger.warning(
                "视觉模型 LM Studio 模型列表暂不可用，继续直接请求 chat/completions: endpoint=%s, model=%s, errors=%s",
                endpoint.name,
                model_name,
                probe.errors,
            )
            return

        logger.info(
            "视觉模型已命中 LM Studio 兼容模式: endpoint=%s, host=%s, detected_by=%s, ttl=%s, loaded=%s",
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

    def _build_headers(self, endpoint_name: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.endpoint_api_keys[endpoint_name]}",
            "Content-Type": "application/json",
        }
