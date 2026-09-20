"""只读核验复用镜像的依赖；不安装、升级或访问网络。"""
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import shutil
import subprocess

from packaging.requirements import Requirement
import yaml


def verify_requirements(paths, *, lookup=version):
    failures = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            requirement = Requirement(line)
            if requirement.url:
                raise ValueError("复用镜像校验不接受外部URL依赖。")
            if requirement.marker and not requirement.marker.evaluate():
                continue
            try:
                installed = lookup(requirement.name)
            except PackageNotFoundError:
                failures.append(f"{requirement.name} 未安装")
                continue
            if not requirement.specifier.contains(installed, prereleases=True):
                failures.append(f"{requirement.name}=={installed} 不满足 {requirement.specifier}")
    if failures:
        raise RuntimeError("运行依赖不兼容：" + "；".join(failures))


def verify_video_commands(frames, *, find=shutil.which, run=subprocess.run):
    for key in ("ffmpeg_path", "ffprobe_path"):
        value = frames.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"视频配置缺少 {key}。")
        executable = find(value)
        if executable is None:
            raise RuntimeError(f"视频可执行文件不可用：{key}")
        run([executable, "-version"], check=True, timeout=10,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    verify_requirements([
        Path("/app/backend/requirements.txt"),
        Path("/app/backend/requirements-video.txt"),
    ])
    # requirements-video只列高层依赖，额外验证原视频链路的核心模块。
    import cv2
    import torch
    import ultralytics

    config = yaml.safe_load(Path("/app/backend/config/workflow.server.yaml").read_text(encoding="utf-8"))
    verify_video_commands(config["input_generation"]["frames"])
    print("应用与视频依赖核验通过；未下载或修改依赖。")
