from typing import Any, Literal, Optional

from pydantic import Field, field_validator, model_validator

from app.schemas.base import StrictModel


class GenerateReportRequest(StrictModel):
    session_id: Optional[str] = None
    input_path: Optional[str] = None
    accident_data: Optional[dict[str, Any]] = None
    video_path: Optional[str] = None
    persist_generated_input: bool = True
    persist_accident_data: bool = False

    @field_validator("session_id", "input_path", "video_path", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @model_validator(mode="after")
    def _validate_input_sources(self) -> "GenerateReportRequest":
        source_count = sum(
            1
            for value in (self.input_path, self.accident_data, self.video_path)
            if value is not None
        )
        if source_count == 0:
            raise ValueError("生成报告时必须提供 accident_data、input_path 或 video_path 其中之一。")
        if source_count > 1:
            raise ValueError("生成报告时 accident_data、input_path 与 video_path 只能提供一种。")
        if self.accident_data is not None and not self.accident_data:
            raise ValueError("accident_data 不能为空对象。")
        return self


class GenerateInputFromVideoRequest(StrictModel):
    video_path: str
    persist_generated_input: bool = True

    @field_validator("video_path")
    @classmethod
    def _validate_video_path(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("video_path 不能为空。")
        return stripped


class GenerateReportResponse(StrictModel):
    trace_id: str
    status: str
    output_dir: str
    guidance: dict[str, Any]
    report: dict[str, Any]
    input_generation: Optional[dict[str, Any]] = None
    initial_knowledge_snippets: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_snippets: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_meta: dict[str, Any] = Field(default_factory=dict)
    agentic_retrieval_rounds: list[dict[str, Any]] = Field(default_factory=list)


class GenerateInputFromVideoResponse(StrictModel):
    status: str
    media_type: str = "video"
    input_path: str
    generated_input: dict[str, Any]
    backup_path: Optional[str] = None
    workspace_dir: str
    yolo_summary_path: Optional[str] = None
    yolo_summary_preview: Optional[dict[str, Any]] = None
    frames_dir: Optional[str] = None
    frame_manifest: list[dict[str, Any]]
    raw_response_path: Optional[str] = None


class GenerateInputFromUploadResponse(StrictModel):
    status: str
    media_type: str
    file_name: Optional[str] = None
    file_names: list[str] = Field(default_factory=list)
    source_count: int = 1
    input_path: str
    generated_input: dict[str, Any]
    backup_path: Optional[str] = None
    workspace_dir: str
    yolo_summary_path: Optional[str] = None
    yolo_summary_preview: Optional[dict[str, Any]] = None
    frames_dir: Optional[str] = None
    frame_manifest: list[dict[str, Any]]
    upload_groups: list[dict[str, Any]] = Field(default_factory=list)
    raw_response_path: Optional[str] = None


class PublicUploadLimitsResponse(StrictModel):
    max_total_bytes: int
    max_image_bytes: int
    max_video_bytes: int
    max_model_images: int
    max_images_per_group: int
    max_videos_per_group: int
    max_total_images: int
    max_total_videos: int


class PublicReportModelOptionResponse(StrictModel):
    label: Literal["max", "pro", "lite"]
    active: bool
    display_name: Optional[str] = None


class PublicReportModelResponse(StrictModel):
    current_label: Optional[Literal["max", "pro", "lite"]] = None
    updated_at: Optional[str] = None
    options: list[PublicReportModelOptionResponse] = Field(default_factory=list)


class PublicAppConfigResponse(StrictModel):
    upload_limits: PublicUploadLimitsResponse
    report_model: PublicReportModelResponse
