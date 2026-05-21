from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.exceptions import ConfigurationError, InputValidationError
from app.core.settings import ReportEndpointSettings, Settings

REPORT_MODEL_LABEL_ORDER = ("max", "pro", "lite")
LEGACY_ENDPOINT_NAME_ALIASES = {
    "duckcoding_opus": "duckcoding_gemini31",
}


class ReportModelSelectionService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._state_path = settings.report_model_selection_state_file_path

    def get_selected_endpoint(self) -> tuple[ReportEndpointSettings, str | None]:
        endpoints = self._get_configured_endpoints()
        default_label = "pro" if len(endpoints) == 1 else None
        state = self._load_state()
        selected_name = str(state.get("selected_endpoint_name") or "").strip()
        selected_name = LEGACY_ENDPOINT_NAME_ALIASES.get(selected_name, selected_name)
        updated_at = self._normalize_timestamp(state.get("updated_at"))

        if selected_name:
            for endpoint in endpoints:
                if endpoint.name == selected_name:
                    self._get_selector_label(endpoint, default_label)
                    return endpoint, updated_at

        default_endpoint = endpoints[0]
        self._get_selector_label(default_endpoint, default_label)
        return default_endpoint, None

    def set_selected_label(self, label: str) -> dict[str, Any]:
        endpoint = self._find_endpoint_by_label(label)
        payload = {
            "selected_endpoint_name": endpoint.name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.get_public_state()

    def get_public_state(self) -> dict[str, Any]:
        selected_endpoint, updated_at = self.get_selected_endpoint()
        endpoints = self._get_configured_endpoints()
        default_label = "pro" if len(endpoints) == 1 else None
        options = sorted(
            [
                {
                    "label": self._get_selector_label(endpoint, default_label),
                    "active": endpoint.name == selected_endpoint.name,
                }
                for endpoint in endpoints
            ],
            key=lambda item: REPORT_MODEL_LABEL_ORDER.index(item["label"]),
        )
        return {
            "current_label": self._get_selector_label(selected_endpoint, default_label),
            "updated_at": updated_at,
            "options": options,
        }

    def get_switchable_labels(self, selected_endpoint_name: str) -> list[str]:
        endpoints = self._get_configured_endpoints()
        default_label = "pro" if len(endpoints) == 1 else None
        labels = [
            self._get_selector_label(endpoint, default_label)
            for endpoint in endpoints
            if endpoint.name != selected_endpoint_name
        ]
        return sorted(labels, key=REPORT_MODEL_LABEL_ORDER.index)

    def _get_configured_endpoints(self) -> list[ReportEndpointSettings]:
        endpoints = self.settings.models.report_external.iter_endpoints_by_priority()
        if not endpoints:
            raise ConfigurationError("report_external.endpoints 至少需要配置一个端点。")
        default_label = "pro" if len(endpoints) == 1 else None
        labels = [self._get_selector_label(endpoint, default_label) for endpoint in endpoints]
        if len(labels) != len(set(labels)):
            raise ConfigurationError("report_external.endpoints 的 selector_label 不能重复。")
        return endpoints

    def _find_endpoint_by_label(self, label: str) -> ReportEndpointSettings:
        normalized = (label or "").strip().lower()
        if not normalized:
            raise InputValidationError("报告模型档位不能为空。")

        matched: ReportEndpointSettings | None = None
        endpoints = self._get_configured_endpoints()
        default_label = "pro" if len(endpoints) == 1 else None
        for endpoint in endpoints:
            if self._get_selector_label(endpoint, default_label) == normalized:
                matched = endpoint
                break

        if matched is None:
            raise InputValidationError(f"不支持的报告模型档位：{label}")
        return matched

    def _get_selector_label(self, endpoint: ReportEndpointSettings, default_label: str | None = None) -> str:
        label = (endpoint.selector_label or "").strip().lower()
        if not label and default_label in REPORT_MODEL_LABEL_ORDER:
            return default_label
        if label not in REPORT_MODEL_LABEL_ORDER:
            raise ConfigurationError(
                f"report_external.endpoints[{endpoint.name}] 缺少合法的 selector_label，必须是 max/pro/lite 之一。"
            )
        return label

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            return {}
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _normalize_timestamp(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None
