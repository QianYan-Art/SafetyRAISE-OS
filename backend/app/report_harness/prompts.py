from pathlib import Path


def load_role_prompts() -> dict[str, str]:
    """只读取随代码发布的固定模板，不接受运行输入中的路径。"""
    root = Path(__file__).resolve().parents[2] / "config" / "report_harness"
    return {role: (root / f"{role}.md").read_text(encoding="utf-8")
            for role in ("generator", "reviewer")}
