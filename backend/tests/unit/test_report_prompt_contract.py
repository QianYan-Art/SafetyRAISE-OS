from pathlib import Path
import re


def test_original_report_template_keeps_business_inputs_without_length_targets():
    """仅验证提示词静态契约，不代表真实模型质量通过。"""
    root = Path(__file__).resolve().parents[2]
    text = (root / "config" / "report_prompt.md").read_text(encoding="utf-8")
    for placeholder in (
        "{在这里粘贴指导意见JSON}",
        "{在这里粘贴结构化事故信息JSON}",
        "{在这里粘贴事故信息关键锚点摘要}",
    ):
        assert placeholder in text
    assert "不设目标字数、最低篇幅或最低段落数" in text
    assert "6000-9000" not in text
    assert re.search(r"(?:至少|不少于)\s*\d+\s*个(?:自然段|分段)", text) is None
    assert "引用法律必须实际阅读条文原文" in text
