from pathlib import Path

import pytest

from app.core.settings import (
    LEGACY_PROMPT_PATHS,
    InputGenerationSettings,
    PromptSettings,
    load_settings,
)


CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.mark.parametrize("config_name", ["workflow.yaml", "workflow.server.yaml"])
def test_bundled_configs_point_at_existing_prompt_files(config_name):
    settings = load_settings(str(CONFIG_DIR / config_name))

    assert settings.guidance_prompt_file.is_file()
    assert settings.report_prompt_file.is_file()
    assert settings.input_generation_prompt_file.is_file()


def test_legacy_prompt_paths_map_to_renamed_files():
    prompts = PromptSettings(
        guidance_prompt_path="backend/config/指导意见生成提示词.md",
        report_prompt_template="backend/config/分析报告生成提示词.md",
    )
    input_generation = InputGenerationSettings(prompt_path="backend/config/事故信息生成提示词.md")

    assert prompts.guidance_prompt_path == "backend/config/guidance_prompt.md"
    assert prompts.report_prompt_template == "backend/config/report_prompt.md"
    assert input_generation.prompt_path == "backend/config/input_generation_prompt.md"
    for replacement in LEGACY_PROMPT_PATHS.values():
        assert (CONFIG_DIR.parent.parent / replacement).is_file()


def test_custom_prompt_paths_are_kept():
    prompts = PromptSettings(guidance_prompt_path="custom/guidance.md", report_prompt_template="custom/report.md")

    assert prompts.guidance_prompt_path == "custom/guidance.md"
    assert prompts.report_prompt_template == "custom/report.md"
