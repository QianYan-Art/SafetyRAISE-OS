from __future__ import annotations

from typing import Literal, Optional

from pydantic import Field, field_validator

from app.schemas.base import StrictModel


ReportModelLabel = Literal["max", "pro", "lite"]


class UserModelConfigRecord(StrictModel):
    label: ReportModelLabel
    display_name: str
    base_url: str
    api_key: str
    configured: bool = True
    provider_name: str = "openai_compatible"
    updated_at: Optional[str] = None


class UpdateUserModelConfigItem(StrictModel):
    label: ReportModelLabel
    display_name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None

    @field_validator("display_name")
    @classmethod
    def _validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) > 24:
            raise ValueError("档位名称不能超过 24 个字符。")
        return normalized or None

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) > 255:
            raise ValueError("模型地址不能超过 255 个字符。")
        return normalized or None

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) > 255:
            raise ValueError("模型密钥不能超过 255 个字符。")
        return normalized or None


class UpdateUserModelConfigsRequest(StrictModel):
    items: list[UpdateUserModelConfigItem] = Field(default_factory=list)
    active_label: Optional[ReportModelLabel] = None


class UserModelConfigStateResponse(StrictModel):
    current_label: Optional[ReportModelLabel] = None
    updated_at: Optional[str] = None
    options: list[UserModelConfigRecord] = Field(default_factory=list)


class UpdateUserModelSelectionRequest(StrictModel):
    label: ReportModelLabel


# ---------------------------------------------------------------------------
# 能力维度模型端点配置（替代 max/pro/lite 三档）
# 详见 .mission/.../feature_capability_model_config_2026-06-01.md
# ---------------------------------------------------------------------------

ModelCapability = Literal["vision", "embedding", "report"]


class EmbeddingTuningParams(StrictModel):
    top_k: Optional[int] = Field(default=None, ge=1, le=50)
    dense_top_k_chunks: Optional[int] = Field(default=None, ge=1, le=50)
    dense_top_k_rules: Optional[int] = Field(default=None, ge=1, le=50)


class CapabilityConfigRecord(StrictModel):
    """读响应：api_key 仅以打码形式回传，明文绝不外泄。"""

    capability: ModelCapability
    configured: bool = False
    base_url: Optional[str] = None
    model_name: Optional[str] = None
    api_key_masked: Optional[str] = None
    params: EmbeddingTuningParams = Field(default_factory=EmbeddingTuningParams)


class UpdateCapabilityConfigItem(StrictModel):
    capability: ModelCapability
    base_url: Optional[str] = None
    model_name: Optional[str] = None
    # 留空或等于打码占位 → 保留原 key；非空 → 覆盖
    api_key: Optional[str] = None
    params: Optional[EmbeddingTuningParams] = None

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if normalized and not (normalized.startswith("http://") or normalized.startswith("https://")):
            raise ValueError("模型地址必须以 http:// 或 https:// 开头。")
        if len(normalized) > 255:
            raise ValueError("模型地址不能超过 255 个字符。")
        return normalized or None

    @field_validator("model_name")
    @classmethod
    def _validate_model_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) > 128:
            raise ValueError("模型名称不能超过 128 个字符。")
        return normalized or None

    @field_validator("api_key")
    @classmethod
    def _validate_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) > 255:
            raise ValueError("模型密钥不能超过 255 个字符。")
        return normalized or None


class UpdateCapabilityConfigsRequest(StrictModel):
    items: list[UpdateCapabilityConfigItem] = Field(default_factory=list)


class CapabilityConfigStateResponse(StrictModel):
    role: str
    capabilities: list[CapabilityConfigRecord] = Field(default_factory=list)
    system_defaults: dict[str, EmbeddingTuningParams] = Field(default_factory=dict)
