import logging
import httpx

from app.core.exceptions import ProviderError
from app.core.settings import ModelEndpointSettings
from app.providers.llm.base import BaseLLMProvider
from app.providers.llm.lmstudio_compat import (
    build_chat_completions_url,
    build_lmstudio_models_urls,
    build_models_url,
    probe_lmstudio_model,
    resolve_lmstudio_compatibility,
)
from app.providers.lmstudio_residency import LMStudioResidencyManager, LMStudioResidencySpec

logger = logging.getLogger(__name__)


class OpenAICompatibleExpertProvider(BaseLLMProvider):
    def __init__(
        self,
        config: ModelEndpointSettings,
        api_key: str | None = None,
        lmstudio_host_allowlist: list[str] | None = None,
        lmstudio_residency_manager: LMStudioResidencyManager | None = None,
        lmstudio_residency_companions: list[LMStudioResidencySpec] | None = None,
    ):
        self.config = config
        self.api_key = (api_key or "").strip()
        self._client = httpx.Client(timeout=config.timeout_seconds)
        self._warmed = False
        self._provider_name = config.provider.strip().lower()
        self._lmstudio_residency_manager = lmstudio_residency_manager
        self._lmstudio_residency_companions = list(lmstudio_residency_companions or [])
        self._lmstudio = resolve_lmstudio_compatibility(
            base_url_or_endpoint=config.base_url,
            provider_name=self._provider_name,
            host_allowlist=lmstudio_host_allowlist,
            ttl_seconds=config.lmstudio_ttl_seconds,
        )
        self._chat_completions_url = self._lmstudio.chat_completions_url
        self._models_url = build_models_url(self._chat_completions_url)
        self._lmstudio_models_urls = build_lmstudio_models_urls(self._chat_completions_url)
        if self._lmstudio.enabled:
            logger.info(
                "专家模型端点已识别为 LM Studio 兼容模式: host=%s, detected_by=%s, ttl=%s",
                self._lmstudio.host,
                self._lmstudio.detected_by,
                self._lmstudio.ttl_seconds,
            )

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self._prewarm_if_needed()
        self._preflight_lmstudio_model_if_needed()
        self._ensure_lmstudio_residency()
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self._lmstudio.enabled and self._lmstudio.ttl_seconds is not None:
            payload["ttl"] = self._lmstudio.ttl_seconds

        try:
            response = self._client.post(
                self._chat_completions_url,
                headers=self._build_headers(),
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            self._warmed = True
            return self._extract_message_content(data)
        except Exception as exc:  # noqa: BLE001
            logger.exception("调用 OpenAI 兼容专家模型失败")
            if self._lmstudio.enabled:
                raise ProviderError(
                    f"LM Studio 专家模型调用失败: {exc}",
                    public_message="专家小模型当前调用失败，请确认目标模型存在且可自动加载后重试。",
                    details={"provider": "lmstudio", "error": str(exc)},
                ) from exc
            raise ProviderError(
                f"OpenAI 兼容专家模型调用失败: {exc}",
                public_message="专家小模型当前调用失败，请稍后重试。",
                details={"provider": self._provider_name or "openai_compatible", "error": str(exc)},
            ) from exc

    def health_check(self) -> bool:
        try:
            for url in self._health_probe_urls():
                try:
                    response = self._client.get(
                        url,
                        headers=self._build_headers(),
                    )
                    response.raise_for_status()
                    return True
                except Exception:  # noqa: BLE001
                    continue
            return False
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        self._client.close()

    def _prewarm_if_needed(self) -> None:
        if self._warmed or not self.config.prewarm_enabled:
            return
        if self._lmstudio.enabled:
            # LM Studio 走 JIT，不额外做预热，避免预热请求反而改变模型驻留状态。
            return
        self._warmed = self.health_check()

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _preflight_lmstudio_model_if_needed(self) -> None:
        if not self._lmstudio.enabled:
            return

        probe = probe_lmstudio_model(
            client=self._client,
            headers=self._build_headers(),
            chat_completions_url=self._chat_completions_url,
            model_name=self.config.model,
        )
        if probe.models_endpoint_accessible and probe.model_exists is False:
            raise ProviderError(
                f"LM Studio 未找到专家模型 `{self.config.model}`。请改用 `/v1/models` 返回的实际模型 id。",
                public_message="专家小模型配置的模型 id 无效，请改用 LM Studio `/v1/models` 返回的真实模型 id。",
                details={"provider": "lmstudio", "model": self.config.model},
            )

        if not probe.models_endpoint_accessible:
            logger.warning(
                "LM Studio 模型列表接口暂不可用，仍继续直接请求 chat/completions: model=%s, errors=%s",
                self.config.model,
                probe.errors,
            )
            return

        logger.info(
            "LM Studio 模型预检通过: model=%s, loaded=%s, models_url=%s",
            self.config.model,
            probe.loaded,
            probe.selected_url,
        )

    def _ensure_lmstudio_residency(self) -> None:
        if self._lmstudio_residency_manager is None or not self._lmstudio.enabled:
            return
        self._lmstudio_residency_manager.ensure_residency(
            primary_spec=LMStudioResidencySpec(
                model=self.config.model,
                base_url_or_endpoint=self.config.base_url,
                api_key=self.api_key,
                provider_name=self._provider_name,
                ttl_seconds=self._lmstudio.ttl_seconds,
            ),
            companion_specs=self._lmstudio_residency_companions,
        )

    def _health_probe_urls(self) -> list[str]:
        if self._lmstudio.enabled:
            return self._lmstudio_models_urls
        return [self._models_url]

    @staticmethod
    def _extract_message_content(payload: dict) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise ProviderError("OpenAI 兼容专家模型返回为空。")

        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = str(item.get("text") or "").strip()
                    if text:
                        text_parts.append(text)
                elif isinstance(item, str) and item.strip():
                    text_parts.append(item.strip())
            return "\n".join(text_parts).strip()
        return str(content).strip()
