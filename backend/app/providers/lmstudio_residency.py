from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Lock

import httpx

from app.providers.llm.lmstudio_compat import (
    build_lmstudio_models_urls,
    extract_model_entries,
    resolve_lmstudio_compatibility,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class LMStudioResidencySpec:
    model: str
    base_url_or_endpoint: str
    api_key: str = ""
    provider_name: str | None = None
    ttl_seconds: int | None = None


class LMStudioResidencyManager:
    _host_locks_guard = Lock()
    _host_locks: dict[str, Lock] = {}

    def __init__(
        self,
        *,
        host_allowlist: list[str] | None,
        resident_limit: int = 2,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.host_allowlist = list(host_allowlist or [])
        self.resident_limit = resident_limit
        self._client = http_client or httpx.Client(timeout=15)
        self._owns_client = http_client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def ensure_residency(
        self,
        *,
        primary_spec: LMStudioResidencySpec,
        companion_specs: list[LMStudioResidencySpec] | None = None,
    ) -> None:
        specs = [primary_spec, *(companion_specs or [])]
        enabled_specs: list[tuple[LMStudioResidencySpec, object]] = []
        desired_models: list[str] = []

        for spec in specs:
            model_name = str(spec.model or "").strip()
            if not model_name:
                continue
            compatibility = resolve_lmstudio_compatibility(
                base_url_or_endpoint=spec.base_url_or_endpoint,
                provider_name=spec.provider_name,
                host_allowlist=self.host_allowlist,
                ttl_seconds=spec.ttl_seconds,
            )
            if not compatibility.enabled:
                continue
            enabled_specs.append((spec, compatibility))
            if model_name not in desired_models:
                desired_models.append(model_name)

        if not enabled_specs:
            return

        if len(desired_models) > self.resident_limit:
            raise ValueError(
                f"LM Studio 驻留目标超过限制：limit={self.resident_limit}, desired={desired_models}"
            )

        grouped_specs: dict[str, dict[str, object]] = {}
        for spec, compatibility in enabled_specs:
            group_key = compatibility.host or compatibility.chat_completions_url
            group = grouped_specs.setdefault(
                group_key,
                {
                    "compatibility": compatibility,
                    "api_key": "",
                },
            )
            if spec.api_key and not group["api_key"]:
                group["api_key"] = spec.api_key

        for group_key, payload in grouped_specs.items():
            compatibility = payload["compatibility"]
            api_key = str(payload["api_key"] or "")
            with self._get_host_lock(group_key):
                models_url, model_entries = self._fetch_native_models(
                    chat_completions_url=compatibility.chat_completions_url,
                    headers=self._build_headers(api_key),
                )
                if not models_url or not model_entries:
                    continue
                unload_targets = self._collect_unload_targets(
                    model_entries=model_entries,
                    desired_models=desired_models,
                )
                for target in unload_targets:
                    self._unload_instance(
                        unload_url=self._build_unload_url(models_url),
                        headers=self._build_headers(api_key),
                        instance_id=target["instance_id"],
                        model_key=target["model_key"],
                    )

    @classmethod
    def _get_host_lock(cls, group_key: str) -> Lock:
        with cls._host_locks_guard:
            if group_key not in cls._host_locks:
                cls._host_locks[group_key] = Lock()
            return cls._host_locks[group_key]

    def _fetch_native_models(
        self,
        *,
        chat_completions_url: str,
        headers: dict[str, str],
    ) -> tuple[str | None, list[dict]]:
        models_urls = [
            url
            for url in build_lmstudio_models_urls(chat_completions_url)
            if "/api/" in url
        ]
        for url in models_urls:
            try:
                response = self._client.get(url, headers=headers)
                response.raise_for_status()
                return url, extract_model_entries(response.json())
            except Exception as exc:  # noqa: BLE001
                logger.warning("LM Studio 模型列表查询失败: url=%s, error=%s", url, exc)
        return None, []

    @staticmethod
    def _collect_unload_targets(
        *,
        model_entries: list[dict],
        desired_models: list[str],
    ) -> list[dict[str, str]]:
        desired = {item.strip() for item in desired_models if item.strip()}
        targets: list[dict[str, str]] = []
        for entry in model_entries:
            model_key = str(entry.get("key") or entry.get("id") or "").strip()
            if not model_key or model_key in desired:
                continue
            loaded_instances = entry.get("loaded_instances")
            if not isinstance(loaded_instances, list):
                continue
            for instance in loaded_instances:
                if not isinstance(instance, dict):
                    continue
                instance_id = str(instance.get("id") or "").strip()
                if not instance_id:
                    continue
                targets.append(
                    {
                        "instance_id": instance_id,
                        "model_key": model_key,
                    }
                )
        return targets

    def _unload_instance(
        self,
        *,
        unload_url: str,
        headers: dict[str, str],
        instance_id: str,
        model_key: str,
    ) -> None:
        try:
            response = self._client.post(
                unload_url,
                headers=headers,
                json={"instance_id": instance_id},
            )
            response.raise_for_status()
            logger.info(
                "LM Studio 驻留管理已卸载非目标模型实例: model=%s, instance_id=%s",
                model_key,
                instance_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "LM Studio 卸载非目标模型实例失败: model=%s, instance_id=%s, error=%s",
                model_key,
                instance_id,
                exc,
            )

    @staticmethod
    def _build_headers(api_key: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    @staticmethod
    def _build_unload_url(models_url: str) -> str:
        normalized = models_url.rstrip("/")
        if normalized.endswith("/models"):
            return f"{normalized}/unload"
        return normalized
