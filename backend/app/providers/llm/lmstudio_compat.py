from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx


@dataclass(slots=True, frozen=True)
class LMStudioCompatibility:
    enabled: bool
    detected_by: str | None
    host: str
    chat_completions_url: str
    ttl_seconds: int | None = None


@dataclass(slots=True)
class LMStudioModelProbe:
    requested_model: str
    models_urls: list[str]
    selected_url: str | None
    models_endpoint_accessible: bool
    model_exists: bool | None
    loaded: bool | None
    model_entry: dict[str, Any] | None
    errors: list[str] = field(default_factory=list)


def build_chat_completions_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        raise ValueError("LM Studio 兼容端点缺少 base_url。")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


def build_models_url(chat_completions_url: str) -> str:
    suffix = "/chat/completions"
    if chat_completions_url.endswith(suffix):
        return chat_completions_url[: -len(suffix)] + "/models"
    return chat_completions_url


def build_lmstudio_models_urls(chat_completions_url: str) -> list[str]:
    normalized = chat_completions_url.rstrip("/")
    suffix = "/v1/chat/completions"
    if not normalized.endswith(suffix):
        return [build_models_url(chat_completions_url)]

    root = normalized[: -len(suffix)]
    return [
        f"{root}/api/v1/models",
        f"{root}/api/v0/models",
        f"{root}/v1/models",
    ]


def resolve_lmstudio_compatibility(
    *,
    base_url_or_endpoint: str,
    provider_name: str | None,
    host_allowlist: Iterable[str] | None,
    ttl_seconds: int | None = None,
) -> LMStudioCompatibility:
    chat_url = build_chat_completions_url(base_url_or_endpoint)
    host = extract_host(chat_url)
    normalized_provider = (provider_name or "").strip().lower()
    normalized_allowlist = normalize_host_allowlist(host_allowlist)

    if normalized_provider == "lmstudio":
        return LMStudioCompatibility(
            enabled=True,
            detected_by="provider",
            host=host,
            chat_completions_url=chat_url,
            ttl_seconds=ttl_seconds,
        )

    if host and host in normalized_allowlist:
        return LMStudioCompatibility(
            enabled=True,
            detected_by="host_allowlist",
            host=host,
            chat_completions_url=chat_url,
            ttl_seconds=ttl_seconds,
        )

    return LMStudioCompatibility(
        enabled=False,
        detected_by=None,
        host=host,
        chat_completions_url=chat_url,
        ttl_seconds=ttl_seconds,
    )


def normalize_host_allowlist(host_allowlist: Iterable[str] | None) -> set[str]:
    normalized: set[str] = set()
    for item in host_allowlist or []:
        candidate = str(item or "").strip().lower()
        if candidate:
            normalized.add(candidate)
    return normalized


def extract_host(url: str) -> str:
    return (urlparse(url).hostname or "").strip().lower()


def probe_lmstudio_model(
    *,
    client: httpx.Client,
    headers: dict[str, str],
    chat_completions_url: str,
    model_name: str,
) -> LMStudioModelProbe:
    requested_model = (model_name or "").strip()
    models_urls = build_lmstudio_models_urls(chat_completions_url)
    errors: list[str] = []
    selected_url: str | None = None
    models_endpoint_accessible = False

    for url in models_urls:
        try:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            models = extract_model_entries(response.json())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
            continue

        models_endpoint_accessible = True
        selected_url = selected_url or url
        model_entry = find_model_entry(models, requested_model)
        if model_entry is not None:
            return LMStudioModelProbe(
                requested_model=requested_model,
                models_urls=models_urls,
                selected_url=url,
                models_endpoint_accessible=True,
                model_exists=True,
                loaded=is_lmstudio_model_loaded(model_entry),
                model_entry=model_entry,
                errors=errors,
            )

    return LMStudioModelProbe(
        requested_model=requested_model,
        models_urls=models_urls,
        selected_url=selected_url,
        models_endpoint_accessible=models_endpoint_accessible,
        model_exists=False if models_endpoint_accessible else None,
        loaded=None,
        model_entry=None,
        errors=errors,
    )


def extract_model_entries(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if isinstance(payload.get("models"), list):
            return [item for item in payload["models"] if isinstance(item, dict)]
        if isinstance(payload.get("data"), list):
            return [item for item in payload["data"] if isinstance(item, dict)]
    raise ValueError("返回格式不合法，缺少 models/data 数组。")


def find_model_entry(models: list[dict[str, Any]], target_model: str) -> dict[str, Any] | None:
    target = (target_model or "").strip()
    for item in models:
        key = str(item.get("key") or item.get("id") or "").strip()
        if key == target:
            return item
    return None


def is_lmstudio_model_loaded(model_entry: dict[str, Any]) -> bool:
    loaded_instances = model_entry.get("loaded_instances")
    if isinstance(loaded_instances, list):
        return bool(loaded_instances)
    if isinstance(loaded_instances, dict):
        return True

    state = str(model_entry.get("state") or model_entry.get("status") or "").strip().lower()
    if state in {"loaded", "ready", "running", "loading"}:
        return True

    if model_entry.get("loaded") is True or model_entry.get("is_loaded") is True:
        return True

    return False

