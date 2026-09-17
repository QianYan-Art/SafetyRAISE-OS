from pathlib import Path
from tempfile import TemporaryDirectory

from app.report_harness.errors import HarnessError
from app.report_harness.release_registry import export_eligibility
from app.services.report_export_service import ReportExportService

ENGINEERING_MARKER = "工程验证样本，不具正式导出资格"


def render_run_export(record: dict, export_format: str, *, mode: str,
                      registry, renderer: ReportExportService,
                      force_engineering: bool = False) -> tuple[bytes, str, str]:
    if record["state"] != "published":
        raise HarnessError("report_not_published")
    if export_format not in {"md", "docx", "pdf"} or mode not in {"formal", "engineering"}:
        raise HarnessError("invalid_export_request", 422)
    if mode == "formal":
        eligible, _ = export_eligibility(record, registry)
        if not eligible:
            raise HarnessError("release_binding_revoked")
    engineering = mode == "engineering" or force_engineering
    marker = ENGINEERING_MARKER if engineering else None
    markdown = record["report"]["report_markdown"]
    if marker:
        markdown = f"> {marker}\n\n" + markdown
    name = f"report-{'engineering-' if engineering else ''}{record['run_id']}.{export_format}"
    if export_format == "md":
        return markdown.encode("utf-8"), renderer.get_media_type("md"), name
    # 不读取或写入旧报告目录，不留下可被旧恢复器误认的文件。
    with TemporaryDirectory(prefix="safetyraise-report-export-") as directory:
        path = Path(directory) / f"report.{export_format}"
        blocks = renderer._parse_markdown_blocks(markdown)
        if export_format == "docx":
            renderer._build_docx(path, blocks, markdown, record["run_id"],
                                 verification_marker=marker)
        else:
            cover = renderer._resolve_pdf_cover_config(blocks, record["run_id"], None)
            renderer._build_pdf(path, blocks, record["run_id"], cover,
                                verification_marker=marker)
        content = path.read_bytes()
    return content, renderer.get_media_type(export_format), name
