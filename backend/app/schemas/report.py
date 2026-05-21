from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.input_generation import InputGenerationArtifact


class ReportSection(BaseModel):
    title: str
    content: str


class ReportResult(BaseModel):
    report_markdown: str
    sections: list[ReportSection] = Field(default_factory=list)
    citations: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)


class ReportArtifact(BaseModel):
    trace_id: str
    guidance: dict[str, Any]
    report: ReportResult
    output_dir: str
    input_generation: InputGenerationArtifact | None = None
    initial_knowledge_snippets: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_snippets: list[dict[str, Any]] = Field(default_factory=list)
    retrieval_meta: dict[str, Any] = Field(default_factory=dict)
    agentic_retrieval_rounds: list[dict[str, Any]] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
