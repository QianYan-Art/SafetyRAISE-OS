from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from app.report_harness.errors import HarnessError
from app.report_harness.release_registry import export_eligibility
from app.services.report_export_service import ReportExportService

ENGINEERING_MARKER = "工程验证样本，不具正式导出资格"
UNREVIEWED_MARKER = "未通过独立审查的候选稿，仅供演示与验收参考，不具正式导出资格"

ExportKind = Literal["formal", "engineering", "unreviewed"]


def available_export(record: dict, registry, *, force_engineering: bool = False) -> ExportKind | None:
    """运行当前可提供的导出方式；运行视图与下载共用同一规则，前端不再自行推导。

    已发布且有有效批准 → formal；已发布但无批准（或强制工程标记）→ engineering；
    审查未闭合但保留了候选稿 → unreviewed；其余状态没有可导出的正文。
    """
    if record.get("state") == "published":
        if not force_engineering and export_eligibility(record, registry)[0]:
            return "formal"
        return "engineering"
    if record.get("state") == "needs_review" and record.get("candidate"):
        return "unreviewed"
    return None


@dataclass(frozen=True)
class _ExportPlan:
    markdown: str
    marker: str | None
    filename_tag: str


def _plan(record: dict, mode: str, registry, force_engineering: bool) -> _ExportPlan:
    kind = available_export(record, registry)
    if kind is None:
        raise HarnessError("report_not_published")
    if mode == "formal":
        # 正式导出只认有效批准；强制工程标记只追加标记，不放宽批准要求。
        if kind == "unreviewed":
            raise HarnessError("report_not_published")
        if kind != "formal":
            raise HarnessError("release_binding_revoked")
        if force_engineering:
            return _ExportPlan(record["report"]["report_markdown"], ENGINEERING_MARKER, "engineering-")
        return _ExportPlan(record["report"]["report_markdown"], None, "")
    if kind == "unreviewed":
        return _ExportPlan(record["candidate"]["report_markdown"], UNREVIEWED_MARKER, "unreviewed-")
    return _ExportPlan(record["report"]["report_markdown"], ENGINEERING_MARKER, "engineering-")


def render_run_export(record: dict, export_format: str, *, mode: str,
                      registry, renderer: ReportExportService,
                      force_engineering: bool = False) -> tuple[bytes, str, str]:
    if export_format not in {"md", "docx", "pdf"} or mode not in {"formal", "engineering"}:
        raise HarnessError("invalid_export_request", 422)
    plan = _plan(record, mode, registry, force_engineering)
    markdown = f"> {plan.marker}\n\n{plan.markdown}" if plan.marker else plan.markdown
    name = f"report-{plan.filename_tag}{record['run_id']}.{export_format}"
    if export_format == "md":
        return markdown.encode("utf-8"), renderer.get_media_type("md"), name
    # 不读取或写入旧报告目录，不留下可被旧恢复器误认的文件。
    with TemporaryDirectory(prefix="safetyraise-report-export-") as directory:
        path = Path(directory) / f"report.{export_format}"
        blocks = renderer._parse_markdown_blocks(markdown)
        if export_format == "docx":
            renderer._build_docx(path, blocks, markdown, record["run_id"],
                                 verification_marker=plan.marker)
        else:
            cover = renderer._resolve_pdf_cover_config(blocks, record["run_id"], None)
            renderer._build_pdf(path, blocks, record["run_id"], cover,
                                verification_marker=plan.marker)
        content = path.read_bytes()
    return content, renderer.get_media_type(export_format), name
