from importlib.metadata import PackageNotFoundError
import importlib.util
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[3] / "deployment/docker/verify-runtime-dependencies.py"
SPEC = importlib.util.spec_from_file_location("verify_runtime_dependencies", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_matching_versions_do_not_install_dependencies(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("# 合成要求\nexample[binary]>=3,<4\n\n", encoding="utf-8")
    seen = []

    def lookup(name):
        seen.append(name)
        return "3.1"

    MODULE.verify_requirements([requirements], lookup=lookup)
    assert seen == ["example"]


def test_missing_or_incompatible_dependency_blocks_build(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("absent>=1\noutdated>=3\n", encoding="utf-8")

    def lookup(name):
        if name == "absent":
            raise PackageNotFoundError(name)
        return "2.9"

    with pytest.raises(RuntimeError, match="absent.*outdated"):
        MODULE.verify_requirements([requirements], lookup=lookup)


def test_inapplicable_platform_marker_is_not_required(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text('unused; python_version < "1"\n', encoding="utf-8")

    def lookup(name):
        pytest.fail("不应查询不适用的平台依赖。")

    MODULE.verify_requirements([requirements], lookup=lookup)


def test_url_dependencies_are_not_silently_accepted(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example @ https://example.invalid/wheel.whl\n", encoding="utf-8")
    with pytest.raises(ValueError, match="URL"):
        MODULE.verify_requirements([requirements], lookup=lambda name: "3.1")


def test_video_commands_use_the_configured_paths_and_must_execute():
    calls = []
    MODULE.verify_video_commands(
        {"ffmpeg_path": "/custom/ffmpeg", "ffprobe_path": "ffprobe"},
        find=lambda value: value,
        run=lambda args, **kwargs: calls.append((args, kwargs)),
    )
    assert [item[0] for item in calls] == [["/custom/ffmpeg", "-version"], ["ffprobe", "-version"]]
    assert all(item[1]["check"] is True and item[1]["timeout"] == 10 for item in calls)


def test_missing_video_binary_blocks_the_runtime_build():
    with pytest.raises(RuntimeError, match="ffprobe_path"):
        MODULE.verify_video_commands(
            {"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe"},
            find=lambda value: None if value == "ffprobe" else value,
            run=lambda *_args, **_kwargs: None,
        )


def test_nonfunctional_video_binary_is_not_ignored():
    import subprocess

    def failure(args, **kwargs):
        raise subprocess.CalledProcessError(1, args)

    with pytest.raises(subprocess.CalledProcessError):
        MODULE.verify_video_commands(
            {"ffmpeg_path": "ffmpeg", "ffprobe_path": "ffprobe"},
            find=lambda value: value, run=failure,
        )
