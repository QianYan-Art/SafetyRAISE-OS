from typing import Any, Optional

from pydantic import BaseModel, Field


class UploadGroupSummary(BaseModel):
    category_id: str
    category_label: str
    category_subtitle: Optional[str] = None
    sequence: int
    image_count: int = 0
    video_count: int = 0
    total_bytes: int = 0
    files: list[dict[str, Any]] = Field(default_factory=list)


class InputGenerationArtifact(BaseModel):
    media_type: str = "video"
    input_path: str
    generated_input: dict[str, Any]
    backup_path: Optional[str] = None
    workspace_dir: str
    yolo_summary_path: Optional[str] = None
    yolo_summary_preview: Optional[dict[str, Any]] = None
    frames_dir: Optional[str] = None
    frame_manifest: list[dict[str, Any]] = Field(default_factory=list)
    upload_groups: list[UploadGroupSummary] = Field(default_factory=list)
    raw_response_path: Optional[str] = None
