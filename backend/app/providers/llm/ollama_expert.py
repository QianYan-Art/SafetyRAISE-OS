import logging

import httpx

from app.core.exceptions import ProviderError
from app.core.settings import ModelEndpointSettings
from app.providers.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)


class OllamaExpertProvider(BaseLLMProvider):
    def __init__(self, config: ModelEndpointSettings):
        self.config = config
        self._client = httpx.Client(timeout=config.timeout_seconds, trust_env=False)
        self._warmed = False

    def generate(self, system_prompt: str, user_prompt: str) -> str:
        self._prewarm_if_needed()
        payload = {
            "model": self.config.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if self.config.keep_alive:
            payload["keep_alive"] = self.config.keep_alive
        url = f"{self.config.base_url.rstrip('/')}/api/chat"
        try:
            resp = self._client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            self._warmed = True
            return data.get("message", {}).get("content", "").strip()
        except Exception as exc:  # noqa: BLE001
            logger.exception("调用 Ollama 失败")
            raise ProviderError(f"Ollama 调用失败: {exc}") from exc

    def health_check(self) -> bool:
        url = f"{self.config.base_url.rstrip('/')}/api/tags"
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
            return True
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        self._client.close()

    def _prewarm_if_needed(self) -> None:
        if self._warmed or not self.config.prewarm_enabled:
            return

        payload = {
            "model": self.config.model,
            "prompt": self.config.warmup_prompt,
            "stream": False,
            "options": {
                "temperature": 0,
                "num_predict": 8,
            },
        }
        if self.config.keep_alive:
            payload["keep_alive"] = self.config.keep_alive

        url = f"{self.config.base_url.rstrip('/')}/api/generate"
        try:
            resp = self._client.post(url, json=payload)
            resp.raise_for_status()
            self._warmed = True
            logger.info("Ollama 预热成功: %s", self.config.model)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ollama 预热失败，将继续直接请求正式生成: %s", exc)
