import os
import re
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.exceptions import ConfigurationError


class RetrySettings(BaseModel):
    max_attempts: int = Field(default=2, ge=1, le=10)
    backoff_seconds: float = Field(default=2, ge=0)


class WorkflowSettings(BaseModel):
    enable_review_node: bool = False
    save_intermediate: bool = True
    retry: RetrySettings = Field(default_factory=RetrySettings)


class AgenticRagSettings(BaseModel):
    enabled: bool = True
    max_rounds: int = Field(default=2, ge=0, le=5)
    top_k_per_round: int = Field(default=3, ge=1, le=10)
    max_total_snippets: int = Field(default=8, ge=1, le=30)
    max_query_chars: int = Field(default=120, ge=20, le=500)


class LocalJsonlRetrievalSettings(BaseModel):
    manifest_path: str = "kbase/data/manifest.json"
    chunks_path: str = "kbase/data/kbase_chunks.jsonl"
    rules_path: str = "kbase/data/liability_rules.jsonl"
    search_index_path: Optional[str] = None
    top_k_chunks: int = Field(default=3, ge=1, le=20)
    top_k_rules: int = Field(default=2, ge=1, le=20)
    max_context_chars: int = Field(default=4000, ge=500, le=20000)
    prefer_enhanced_rules: bool = True
    enable_search_index: bool = True
    watch_manifest_changes: bool = True
    fallback_mock_on_error: bool = True


class HybridRetrievalSettings(BaseModel):
    dense_manifest_path: str = "kbase/data/dense_manifest.json"
    dense_records_path: str = "kbase/data/dense_records.jsonl"
    dense_vectors_path: str = "kbase/data/dense_vectors.f16.npy"
    sparse_top_k_chunks: int = Field(default=8, ge=1, le=50)
    sparse_top_k_rules: int = Field(default=12, ge=1, le=50)
    dense_top_k_chunks: int = Field(default=8, ge=1, le=50)
    dense_top_k_rules: int = Field(default=12, ge=1, le=50)
    rrf_merge_top_k: int = Field(default=24, ge=1, le=100)
    rerank_top_k: int = Field(default=6, ge=1, le=20)
    final_context_top_k: int = Field(default=6, ge=1, le=20)
    max_context_chars: int = Field(default=5200, ge=500, le=20000)
    agentic_sparse_top_k_chunks: int = Field(default=4, ge=1, le=50)
    agentic_sparse_top_k_rules: int = Field(default=6, ge=1, le=50)
    agentic_dense_top_k_chunks: int = Field(default=4, ge=1, le=50)
    agentic_dense_top_k_rules: int = Field(default=6, ge=1, le=50)
    agentic_rrf_merge_top_k: int = Field(default=12, ge=1, le=100)
    agentic_rerank_top_k: int = Field(default=3, ge=1, le=20)


class RetrievalSettings(BaseModel):
    provider: str = "mock"
    top_k: int = Field(default=5, ge=1, le=50)
    min_score: float = Field(default=0.2, ge=0, le=1)
    local_jsonl: LocalJsonlRetrievalSettings = Field(default_factory=LocalJsonlRetrievalSettings)
    hybrid: HybridRetrievalSettings = Field(default_factory=HybridRetrievalSettings)
    agentic: AgenticRagSettings = Field(default_factory=AgenticRagSettings)


class PromptSettings(BaseModel):
    guidance_prompt_path: str
    report_prompt_template: str


class InputGenerationYoloSettings(BaseModel):
    python_executable: str = ".venv/Scripts/python.exe"
    runner_script: str = "backend/app/tools/extract_video_features.py"
    model_path: str = "models/yolo11n.pt"
    tracker: str = "bytetrack.yaml"
    confidence: float = Field(default=0.3, ge=0, le=1)
    device: Optional[str] = None
    relevant_classes: list[str] = Field(
        default_factory=lambda: ["person", "bicycle", "car", "motorcycle", "bus", "truck"]
    )
    max_track_summaries: int = Field(default=12, ge=1, le=50)


class InputGenerationFrameSettings(BaseModel):
    ffmpeg_path: str = "ffmpeg.exe"
    ffprobe_path: str = "ffprobe.exe"
    max_video_seconds: float = Field(default=20.0, gt=1, le=120)
    min_frames: int = Field(default=8, ge=4, le=24)
    base_frames: int = Field(default=6, ge=0, le=24)
    frames_per_second: float = Field(default=0.75, ge=0.1, le=4)
    max_frames: int = Field(default=12, ge=4, le=24)
    anchor_frames: int = Field(default=5, ge=3, le=8)
    uniform_frames: int = Field(default=8, ge=1, le=24)
    event_frames: int = Field(default=4, ge=0, le=12)
    min_frame_gap: int = Field(default=3, ge=1, le=50)
    active_window_padding_seconds: float = Field(default=1.2, ge=0, le=5)
    event_window_before_seconds: float = Field(default=0.6, ge=0, le=5)
    event_window_after_seconds: float = Field(default=0.8, ge=0, le=5)
    max_side: int = Field(default=960, ge=320, le=4096)
    jpeg_quality: int = Field(default=3, ge=2, le=31)


class InputGenerationUploadSettings(BaseModel):
    max_files: int = Field(default=140, ge=1, le=256)
    max_total_bytes: int = Field(default=1073741824, ge=1048576, le=2147483648)
    max_image_bytes: int = Field(default=10485760, ge=1048576, le=104857600)
    max_video_bytes: int = Field(default=104857600, ge=1048576, le=2147483648)
    max_model_images: int = Field(default=48, ge=4, le=96)
    max_images_per_group: int = Field(default=20, ge=1, le=100)
    max_videos_per_group: int = Field(default=5, ge=1, le=50)
    max_total_images: int = Field(default=120, ge=1, le=500)
    max_total_videos: int = Field(default=20, ge=1, le=200)


class InputGenerationSettings(BaseModel):
    generated_input_path: str = "backend/data/input_accident.json"
    backup_dir: str = "backend/data/backup"
    workspace_dir: str = "backend/data/input_generation"
    prompt_path: str = "backend/config/事故信息生成提示词.md"
    template_path: str = "backend/config/input_accident_template.json"
    retain_debug_artifacts: bool = False
    retain_workspace_count: int = Field(default=2, ge=1, le=20)
    yolo: InputGenerationYoloSettings = Field(default_factory=InputGenerationYoloSettings)
    frames: InputGenerationFrameSettings = Field(default_factory=InputGenerationFrameSettings)
    upload: InputGenerationUploadSettings = Field(default_factory=InputGenerationUploadSettings)

    @model_validator(mode="after")
    def _validate_frame_budget(self) -> "InputGenerationSettings":
        if self.frames.min_frames > self.frames.max_frames:
            raise ValueError("input_generation.frames.min_frames 不能大于 max_frames。")
        if self.frames.anchor_frames > self.frames.max_frames:
            raise ValueError("input_generation.frames.anchor_frames 不能大于 max_frames。")
        return self


class ModelEndpointSettings(BaseModel):
    provider: str
    model: str
    base_url: str
    api_key_env: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: Optional[int] = Field(default=None, ge=128)
    timeout_seconds: int = Field(default=120, ge=5)
    keep_alive: Optional[str] = None
    lmstudio_ttl_seconds: Optional[int] = Field(default=None, ge=1)
    prewarm_enabled: bool = True
    warmup_prompt: str = "请只回复 OK"


class RetrievalEmbeddingModelSettings(BaseModel):
    provider: str = "openai_compatible"
    model: str = "text-embedding-qwen3-embedding-0.6b"
    base_url: str = "http://127.0.0.1:1234/v1"
    api_key_env: Optional[str] = None
    # 内联 key：仅供 per-user 嵌入 override 使用（优先于 api_key_env）
    api_key: Optional[str] = None
    timeout_seconds: int = Field(default=120, ge=5)
    lmstudio_ttl_seconds: Optional[int] = Field(default=None, ge=1)
    dimensions: int = Field(default=1024, ge=1, le=32768)
    query_instruction: str = (
        "Given a Chinese traffic accident analysis query, retrieve statutory rules, "
        "liability rules, traffic-control passages, and scenario guidance that support "
        "factual analysis and responsibility determination."
    )
    query_max_length: int = Field(default=512, ge=32, le=32768)
    document_max_length: int = Field(default=2048, ge=32, le=32768)
    cache_size: int = Field(default=512, ge=1, le=4096)
    cache_ttl_seconds: int = Field(default=21600, ge=60, le=604800)


class RetrievalRerankerModelSettings(BaseModel):
    enabled: bool = True
    provider: str = "tei"
    model: str = "Alibaba-NLP/gte-multilingual-reranker-base"
    base_url: str = "http://127.0.0.1:8081"
    api_key_env: Optional[str] = None
    timeout_seconds: int = Field(default=180, ge=5)


class ModelsSettings(BaseModel):
    expert_local: ModelEndpointSettings
    report_external: "ReportModelSettings"
    accident_vision: "ReportModelSettings"
    retrieval_embedding: RetrievalEmbeddingModelSettings = Field(default_factory=RetrievalEmbeddingModelSettings)
    retrieval_reranker: RetrievalRerankerModelSettings = Field(default_factory=RetrievalRerankerModelSettings)


class ReportRetrySettings(BaseModel):
    max_attempts_per_endpoint: int = Field(default=2, ge=1, le=10)
    backoff_seconds: float = Field(default=2, ge=0)


class ReasoningSettings(BaseModel):
    effort: Optional[Literal["none", "minimal", "low", "medium", "high", "xhigh"]] = None
    max_tokens: Optional[int] = Field(default=None, ge=1)
    enabled: Optional[bool] = None
    exclude: Optional[bool] = None


class ReportEndpointConnectionSettings(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    connection_type: Optional[str] = Field(default=None, alias="_type")
    key: Optional[str] = None
    url: Optional[str] = None


class ReportEndpointSettings(BaseModel):
    name: str = "primary"
    priority: Optional[int] = None
    connection: Optional[ReportEndpointConnectionSettings] = None
    url: str
    model: Optional[str] = None
    timeout_seconds: int = Field(default=120, ge=5)
    api_key_env: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    lmstudio_ttl_seconds: Optional[int] = Field(default=None, ge=1)
    verbosity: Optional[Literal["low", "medium", "high"]] = None
    reasoning_effort: Optional[Literal["none", "minimal", "low", "medium", "high", "xhigh"]] = None
    reasoning: Optional[ReasoningSettings] = None
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_reasoning_overrides(self) -> "ReportEndpointSettings":
        if self.reasoning is not None and self.reasoning_effort is not None:
            raise ValueError("同一个端点不能同时配置 reasoning 和 reasoning_effort。")
        return self


class ReportModelSettings(BaseModel):
    provider: str
    model: Optional[str] = None
    api_key_env: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    max_tokens: int = Field(default=4000, ge=128)
    endpoints: list[ReportEndpointSettings] = Field(default_factory=list)
    retry: ReportRetrySettings = Field(default_factory=ReportRetrySettings)

    def iter_endpoints_by_priority(self) -> list[ReportEndpointSettings]:
        indexed = list(enumerate(self.endpoints))
        indexed.sort(
            key=lambda item: (
                item[1].priority is None,
                item[1].priority if item[1].priority is not None else item[0],
                item[0],
            )
        )
        return [endpoint for _, endpoint in indexed]

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_single_endpoint_config(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        if data.get("endpoints"):
            return data

        base_url = data.get("base_url")
        if not base_url:
            return data

        timeout_seconds = data.get("timeout_seconds", 120)
        api_key_env = data.get("api_key_env")
        upgraded = dict(data)
        upgraded["endpoints"] = [
            {
                "name": "primary",
                "url": f"{str(base_url).rstrip('/')}/chat/completions",
                "timeout_seconds": timeout_seconds,
                "api_key_env": api_key_env,
            }
        ]
        return upgraded

    @model_validator(mode="after")
    def _validate_endpoints(self) -> "ReportModelSettings":
        if not self.endpoints:
            raise ValueError("report_external.endpoints 至少需要配置一个端点。")
        names = [item.name for item in self.endpoints]
        if len(names) != len(set(names)):
            raise ValueError("report_external.endpoints 的 name 不能重复。")
        for endpoint in self.endpoints:
            if not (endpoint.model or self.model):
                raise ValueError(
                    f"report_external.endpoints[{endpoint.name}] 缺少 model，且未配置全局默认 model。"
                )
        return self


class InputSettings(BaseModel):
    adapter: str = "file"
    file_path: str = "backend/data/input_accident.json"


class AppSettings(BaseModel):
    env: str = "dev"
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    output_dir: str = "backend/data/output"
    output_retain_count: int = Field(default=0, ge=0, le=10000)
    chat_sessions_dir: str = "backend/data/chat_sessions"
    lmstudio_host_allowlist: list[str] = Field(default_factory=list)
    lmstudio_resident_limit: int = Field(default=2, ge=1, le=4)


class DatabaseSettings(BaseModel):
    dsn: str = "postgresql://<DB_USER>:<DB_PASSWORD>@127.0.0.1:5432/safetyraise"
    min_pool_size: int = Field(default=1, ge=1, le=10)
    max_pool_size: int = Field(default=4, ge=1, le=20)
    connect_timeout_seconds: int = Field(default=10, ge=1, le=60)


DEFAULT_JWT_SECRET = "change-me-in-production"


class AuthSettings(BaseModel):
    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = Field(default=720, ge=5, le=43200)
    bootstrap_admin_username: str = "safetyraise"
    bootstrap_admin_password: str = "SafetyRaise@2026"
    bootstrap_admin_display_name: str = "SafetyRAISE 管理员"
    # 生产 profile 置 true：启动时若 jwt_secret 仍是公开默认串则 fail-fast，
    # 防止某次部署丢失 AUTH_JWT_SECRET 后静默回落公开默认串（可被伪造 admin）。
    require_strong_secret: bool = False

    def jwt_secret_is_insecure(self) -> bool:
        secret = str(self.jwt_secret or "").strip()
        return not secret or secret == DEFAULT_JWT_SECRET


class Settings(BaseModel):
    app: AppSettings
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    input: InputSettings
    models: ModelsSettings
    prompts: PromptSettings
    input_generation: InputGenerationSettings
    retrieval: RetrievalSettings
    workflow: WorkflowSettings

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    @property
    def output_dir_path(self) -> Path:
        return (self.project_root / self.app.output_dir).resolve()

    @property
    def backend_data_dir_path(self) -> Path:
        return (self.project_root / "backend" / "data").resolve()

    @property
    def chat_sessions_dir_path(self) -> Path:
        return self.resolve_path(self.app.chat_sessions_dir)

    @property
    def guidance_prompt_file(self) -> Path:
        return self.resolve_path(self.prompts.guidance_prompt_path)

    @property
    def report_prompt_file(self) -> Path:
        return self.resolve_path(self.prompts.report_prompt_template)

    @property
    def input_generation_prompt_file(self) -> Path:
        return self.resolve_path(self.input_generation.prompt_path)

    @property
    def input_generation_template_file(self) -> Path:
        return self.resolve_path(self.input_generation.template_path)

    @property
    def input_generation_output_file(self) -> Path:
        return self.resolve_path(self.input_generation.generated_input_path)

    @property
    def input_generation_backup_dir_path(self) -> Path:
        return self.resolve_path(self.input_generation.backup_dir)

    @property
    def input_generation_workspace_dir_path(self) -> Path:
        return self.resolve_path(self.input_generation.workspace_dir)

    def resolve_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path.resolve()
        return (self.project_root / path).resolve()


def load_settings(config_path: Optional[str] = None) -> Settings:
    resolved = resolve_config_path(config_path)
    if not resolved.exists():
        raise ConfigurationError(f"配置文件不存在: {resolved}")

    with resolved.open("r", encoding="utf-8") as f:
        payload = yaml.safe_load(f) or {}

    payload = _expand_env_placeholders(payload)

    settings = Settings.model_validate(payload)
    settings.output_dir_path.mkdir(parents=True, exist_ok=True)
    settings.input_generation_backup_dir_path.mkdir(parents=True, exist_ok=True)
    settings.input_generation_workspace_dir_path.mkdir(parents=True, exist_ok=True)
    return settings


def get_api_key(env_name: Optional[str]) -> str:
    if not env_name:
        raise ConfigurationError("未配置 API Key 环境变量名称。")

    value = os.getenv(env_name, "").strip()
    if not value:
        if _looks_like_url(env_name):
            raise ConfigurationError(f"API Key 配置项看起来像 URL，而不是环境变量名或字面量密钥: {env_name}")
        if _looks_like_literal_api_key(env_name):
            return env_name.strip()
        raise ConfigurationError(f"环境变量 {env_name} 未设置。")
    return value


def resolve_config_path(config_path: Optional[str] = None) -> Path:
    if config_path:
        return Path(config_path).resolve()

    env_path = os.getenv("WORKFLOW_CONFIG_PATH")
    if env_path:
        return Path(env_path).resolve()

    return (Path(__file__).resolve().parents[2] / "config" / "workflow.yaml").resolve()


def _looks_like_literal_api_key(value: str) -> bool:
    candidate = (value or "").strip()
    if not candidate:
        return False
    if _looks_like_url(candidate):
        return False
    if candidate.startswith("sk-") and len(candidate) >= 16:
        return True
    if len(candidate) < 16:
        return False
    if any(char in candidate for char in ".:="):
        return True
    if "-" in candidate and any(char.islower() for char in candidate):
        return True
    return False


def _looks_like_url(value: str) -> bool:
    candidate = (value or "").strip().lower()
    return candidate.startswith("http://") or candidate.startswith("https://")


ENV_PLACEHOLDER_RE = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")


def _expand_env_placeholders(payload: object) -> object:
    if isinstance(payload, dict):
        return {key: _expand_env_placeholders(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [_expand_env_placeholders(item) for item in payload]
    if isinstance(payload, str):
        return _expand_env_value(payload)
    return payload


def _expand_env_value(raw_value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        env_name = match.group(1)
        default = match.group(2) if match.group(2) is not None else ""
        return os.getenv(env_name, default)

    return ENV_PLACEHOLDER_RE.sub(replace, raw_value)
