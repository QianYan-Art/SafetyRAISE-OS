import json
import logging
import math
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.exceptions import ConfigurationError, InputValidationError, ProviderError
from app.core.json_parser import extract_json_from_text
from app.core.settings import Settings
from app.providers.llm.openai_vision import OpenAIVisionProvider
from app.schemas.input_generation import InputGenerationArtifact, UploadGroupSummary

logger = logging.getLogger(__name__)

YOLO_SUMMARY_PLACEHOLDER = "{在这里粘贴YOLO结构化摘要JSON}"
FRAME_MANIFEST_PLACEHOLDER = "{在这里粘贴关键帧清单JSON}"
INPUT_TEMPLATE_PLACEHOLDER = "{在这里粘贴input_accident模板JSON}"
GROUPED_MATERIALS_PLACEHOLDER = "{在这里粘贴按分组整理的事故材料摘要JSON}"


class InputGenerationService:
    def __init__(self, settings: Settings, vision_provider: OpenAIVisionProvider):
        self.settings = settings
        self.vision_provider = vision_provider
        self._input_template = self._load_input_template()

    def generate(
        self,
        video_path: str,
        persist_generated_input: bool = True,
    ) -> InputGenerationArtifact:
        return self.generate_from_media(
            video_paths=[video_path],
            persist_generated_input=persist_generated_input,
        )

    def generate_from_images(
        self,
        image_paths: list[str],
        persist_generated_input: bool = True,
    ) -> InputGenerationArtifact:
        return self.generate_from_media(
            image_paths=image_paths,
            persist_generated_input=persist_generated_input,
        )

    def generate_from_media(
        self,
        image_paths: list[str] | None = None,
        video_paths: list[str] | None = None,
        media_entries: list[dict[str, Any]] | None = None,
        group_definitions: list[dict[str, Any]] | None = None,
        existing_accident_data: dict[str, Any] | None = None,
        persist_generated_input: bool = True,
    ) -> InputGenerationArtifact:
        resolved_media_entries = self._coerce_media_entries(
            image_paths=image_paths,
            video_paths=video_paths,
            media_entries=media_entries,
        )
        if not resolved_media_entries:
            raise InputValidationError("至少需要上传一张事故图片或一个事故视频。")

        normalized_existing = None
        if existing_accident_data:
            normalized_existing = self._normalize_generated_input(existing_accident_data)

        workspace_dir = self._create_workspace_dir()
        frames_dir = workspace_dir / "frames"
        uploads_dir = workspace_dir / "uploads"
        yolo_root_dir = workspace_dir / "yolo"
        frames_dir.mkdir(parents=True, exist_ok=True)
        uploads_dir.mkdir(parents=True, exist_ok=True)
        if any(item["media_type"] == "video" for item in resolved_media_entries):
            yolo_root_dir.mkdir(parents=True, exist_ok=True)

        prepared_entries, upload_group_summaries = self._prepare_media_inputs(
            media_entries=resolved_media_entries,
            uploads_dir=uploads_dir,
        )
        normalized_group_definitions = self._normalize_group_definitions(
            group_definitions=group_definitions,
            prepared_entries=prepared_entries,
        )
        upload_group_summaries = self._hydrate_upload_group_summaries(
            group_definitions=normalized_group_definitions,
            upload_groups=upload_group_summaries,
        )
        self._write_json(
            workspace_dir / "upload_manifest.json",
            {
                "groups": normalized_group_definitions,
                "items": prepared_entries,
            },
        )

        image_entries = [item for item in prepared_entries if item["media_type"] == "image"]
        video_entries = [item for item in prepared_entries if item["media_type"] == "video"]
        image_frame_candidates = self._build_image_frame_candidates(image_entries)
        self._write_json(workspace_dir / "key_frame_manifest.json", [])
        video_contexts: list[dict[str, Any]] = []
        prompt_sources: list[dict[str, Any]] = []
        combined_yolo_sources: list[dict[str, Any]] = []
        key_frame_manifest: list[dict[str, Any]] = []

        if image_entries:
            image_summary = self._build_image_prompt_summary(image_entries)
            prompt_sources.append(image_summary)

        for index, video_entry in enumerate(video_entries, start=1):
            resolved_video_path = self.settings.resolve_path(str(video_entry["path"]))
            if not resolved_video_path.exists():
                raise InputValidationError(f"事故视频不存在: {resolved_video_path.name}")

            probed_duration = self._probe_video_duration_seconds(resolved_video_path)
            yolo_output_dir = (
                yolo_root_dir
                / str(video_entry["category_id"])
                / f"{int(video_entry['group_sequence']):02d}-{Path(str(video_entry['path'])).stem}"
            )
            yolo_output_dir.mkdir(parents=True, exist_ok=True)
            yolo_summary = self._run_yolo_extractor(resolved_video_path, yolo_output_dir)
            actual_duration = float(yolo_summary.get("video", {}).get("duration_seconds", 0.0) or probed_duration)
            video_frames = self._extract_key_frames(
                video_path=resolved_video_path,
                yolo_summary=yolo_summary,
                frames_dir=frames_dir / str(video_entry["category_id"]),
                filename_prefix=f"video_{index:02d}_frame",
                source_name=str(video_entry["original_name"]),
                media_type="video",
                category_id=str(video_entry["category_id"]),
                category_label=str(video_entry["category_label"]),
                category_subtitle=str(video_entry.get("category_subtitle") or ""),
                category_sequence=int(video_entry.get("category_sequence", 0) or 0),
                sequence=int(video_entry["sequence"]),
            )
            key_frame_manifest.extend(video_frames)
            video_contexts.append(
                {
                    "video_path": resolved_video_path,
                    "source_name": str(video_entry["original_name"]),
                    "category_id": str(video_entry["category_id"]),
                    "category_label": str(video_entry["category_label"]),
                    "category_subtitle": video_entry.get("category_subtitle"),
                    "category_sequence": int(video_entry.get("category_sequence", 0) or 0),
                    "group_sequence": int(video_entry["group_sequence"]),
                    "yolo_output_dir": yolo_output_dir,
                    "yolo_summary": yolo_summary,
                    "duration_seconds": max(actual_duration, 0.1),
                }
            )
            video_summary = self._build_prompt_summary(yolo_summary)
            video_summary.update(
                {
                    "media_type": "video",
                    "source_name": str(video_entry["original_name"]),
                    "category_id": str(video_entry["category_id"]),
                    "category_label": str(video_entry["category_label"]),
                    "category_subtitle": str(video_entry.get("category_subtitle") or ""),
                    "category_sequence": int(video_entry.get("category_sequence", 0) or 0),
                }
            )
            prompt_sources.append(video_summary)
            combined_yolo_sources.append(
                {
                    "source_name": str(video_entry["original_name"]),
                    "category_id": str(video_entry["category_id"]),
                    "category_label": str(video_entry["category_label"]),
                    "category_subtitle": str(video_entry.get("category_subtitle") or ""),
                    "category_sequence": int(video_entry.get("category_sequence", 0) or 0),
                    "sequence": int(video_entry["sequence"]),
                    "summary_path": str((yolo_output_dir / "summary.json").resolve()),
                    "summary": yolo_summary,
                }
            )

        self._write_json(workspace_dir / "key_frame_manifest.json", key_frame_manifest)
        frame_manifest = self._select_model_frame_manifest(
            image_entries=image_frame_candidates,
            key_frames=key_frame_manifest,
            upload_groups=upload_group_summaries,
        )
        prompt_summary = self._build_generation_prompt_summary(
            prompt_sources=prompt_sources,
            has_images=bool(image_entries),
            has_videos=bool(video_contexts),
        )
        grouped_materials_summary = self._build_grouped_materials_prompt_summary(
            group_definitions=normalized_group_definitions,
            prepared_entries=prepared_entries,
            frame_manifest=frame_manifest,
            key_frame_manifest=key_frame_manifest,
            combined_yolo_sources=combined_yolo_sources,
        )
        system_prompt = self._render_generation_prompt(
            prompt_summary,
            frame_manifest,
            grouped_materials_summary,
        )
        system_prompt = self._augment_system_prompt_with_existing_data(system_prompt, normalized_existing)
        user_prompt = self._build_generation_user_prompt(
            has_images=bool(image_entries),
            has_videos=bool(video_contexts),
            has_existing_context=normalized_existing is not None,
        )
        image_paths_for_model = [Path(item["path"]) for item in frame_manifest]
        image_captions = self._build_image_captions(frame_manifest)

        raw_response = self.vision_provider.generate(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_paths=image_paths_for_model,
            image_captions=image_captions,
        )
        generated_input, raw_response = self._parse_vision_json(
            raw_response=raw_response,
            system_prompt=system_prompt,
            image_paths=image_paths_for_model,
            image_captions=image_captions,
        )
        if normalized_existing:
            generated_input = self._merge_existing_accident_data(normalized_existing, generated_input)

        yolo_summary_path = self._write_combined_yolo_summary(
            yolo_root_dir=yolo_root_dir,
            has_images=bool(image_entries),
            prompt_sources=prompt_sources,
            combined_yolo_sources=combined_yolo_sources,
        )
        yolo_summary_preview = self._build_yolo_summary_preview(
            prompt_sources=prompt_sources,
            combined_yolo_sources=combined_yolo_sources,
            has_images=bool(image_entries),
        )
        artifact_frames_dir = (
            workspace_dir
            if image_entries and video_contexts
            else frames_dir
            if video_contexts
            else uploads_dir
            if image_entries
            else None
        )

        return self._finalize_generation_artifact(
            workspace_dir=workspace_dir,
            media_type=self._resolve_media_type(bool(image_entries), bool(video_contexts)),
            generated_input=generated_input,
            frame_manifest=frame_manifest,
            persist_generated_input=persist_generated_input,
            frames_dir=artifact_frames_dir,
            yolo_summary_path=yolo_summary_path,
            yolo_summary_preview=yolo_summary_preview,
            upload_groups=upload_group_summaries,
            system_prompt=system_prompt,
            raw_response=raw_response,
        )

    def close(self) -> None:
        self.vision_provider.close()

    def _run_yolo_extractor(self, video_path: Path, output_dir: Path) -> dict[str, Any]:
        yolo_settings = self.settings.input_generation.yolo
        python_executable = self._resolve_runtime_path(yolo_settings.python_executable)
        runner_script = self.settings.resolve_path(yolo_settings.runner_script)
        model_path = self.settings.resolve_path(yolo_settings.model_path)

        if not python_executable.exists():
            raise ConfigurationError(
                f"YOLO Python 解释器不存在: {python_executable}。"
                "请使用 uv 在项目 .venv 中安装视频依赖。"
            )
        if not runner_script.exists():
            raise ConfigurationError(f"YOLO 提取脚本不存在: {runner_script}")
        if not model_path.exists():
            raise ConfigurationError(f"YOLO 权重不存在: {model_path}")

        command = [
            str(python_executable),
            str(runner_script),
            "--video",
            str(video_path),
            "--output-dir",
            str(output_dir),
            "--model",
            str(model_path),
            "--conf",
            str(yolo_settings.confidence),
            "--tracker",
            yolo_settings.tracker,
            "--max-track-summaries",
            str(yolo_settings.max_track_summaries),
        ]
        if yolo_settings.device:
            command.extend(["--device", yolo_settings.device])
        if yolo_settings.relevant_classes:
            command.extend(["--classes", *yolo_settings.relevant_classes])

        result = subprocess.run(
            command,
            cwd=str(self.settings.project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if self.settings.input_generation.retain_debug_artifacts:
            (output_dir / "runner_stdout.log").write_text(result.stdout or "", encoding="utf-8")
            (output_dir / "runner_stderr.log").write_text(result.stderr or "", encoding="utf-8")

        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            if "ModuleNotFoundError" in message:
                raise ProviderError(
                    "YOLO 特征提取运行失败，当前项目虚拟环境缺少视频依赖。"
                    "请优先使用 uv 安装 backend/requirements-video.txt。"
                ) from None
            raise ProviderError(
                "YOLO 特征提取失败，请检查视频分析依赖是否完整后重试。",
                details={"stderr": message},
            )

        summary_path = output_dir / "summary.json"
        if not summary_path.exists():
            raise ProviderError(f"YOLO 特征提取未生成 summary.json: {summary_path}")
        return json.loads(summary_path.read_text(encoding="utf-8"))

    def _extract_key_frames(
        self,
        video_path: Path,
        yolo_summary: dict[str, Any],
        frames_dir: Path,
        target_total_override: int | None = None,
        filename_prefix: str = "frame",
        source_name: str | None = None,
        media_type: str = "video",
        category_id: str = "",
        category_label: str = "",
        category_subtitle: str = "",
        category_sequence: int = 0,
        sequence: int = 0,
    ) -> list[dict[str, Any]]:
        frame_settings = self.settings.input_generation.frames
        video_meta = yolo_summary.get("video", {})
        frame_count = int(video_meta.get("frame_count", 0))
        if frame_count <= 0:
            raise ProviderError("YOLO 摘要缺少有效帧数，无法执行抽帧。")

        fps = float(video_meta.get("fps", 0.0) or 10.0)
        duration_seconds = float(video_meta.get("duration_seconds", 0.0) or (frame_count / fps))
        width = int(yolo_summary.get("video", {}).get("width", 0))
        frame_summaries = list(yolo_summary.get("frame_summaries") or [])
        event_candidates = list(yolo_summary.get("event_candidates") or [])
        target_total = (
            target_total_override
            if target_total_override is not None
            else self._resolve_target_frame_count(duration_seconds, frame_count)
        )
        target_total = max(min(target_total, frame_count), 1)
        focus_start, focus_end = self._resolve_focus_window(frame_summaries, frame_count, fps)

        selected_frames: dict[int, str] = {}
        anchor_indexes = self._build_uniform_frame_indexes_for_range(
            focus_start,
            focus_end,
            min(frame_settings.anchor_frames, target_total),
        )
        for index, frame in enumerate(anchor_indexes):
            self._register_frame(
                selected_frames,
                frame,
                self._resolve_anchor_reason(index, len(anchor_indexes)),
                frame_settings.min_frame_gap,
            )

        before_offset = max(int(round(frame_settings.event_window_before_seconds * fps)), 1)
        after_offset = max(int(round(frame_settings.event_window_after_seconds * fps)), 1)
        event_seed_gap = max(
            frame_settings.min_frame_gap * 2,
            max((before_offset + after_offset) // 2, 1),
        )
        event_seeds: list[int] = []
        for item in event_candidates:
            if len(event_seeds) >= frame_settings.event_frames or len(selected_frames) >= target_total:
                break

            frame = int(item.get("frame", 0))
            event_score = float(item.get("event_score", 0.0) or 0.0)
            object_count = int(item.get("object_count", 0) or 0)
            if frame < focus_start or frame > focus_end:
                continue
            if event_score <= 0 and object_count <= 0:
                continue
            if any(abs(existing - frame) < event_seed_gap for existing in event_seeds):
                continue

            event_seeds.append(frame)
            base_reason = str(item.get("reason") or "事故关键过程")
            event_window = [
                (max(frame - before_offset, 1), f"{base_reason}（事故前）"),
                (frame, f"{base_reason}（事故中）"),
                (min(frame + after_offset, frame_count), f"{base_reason}（事故后）"),
            ]
            for candidate_frame, reason in event_window:
                if len(selected_frames) >= target_total:
                    break
                self._register_frame(
                    selected_frames,
                    candidate_frame,
                    reason,
                    frame_settings.min_frame_gap,
                )

        focus_uniform_indexes = self._build_uniform_frame_indexes_for_range(
            focus_start,
            focus_end,
            max(frame_settings.uniform_frames, target_total * 2),
        )
        for frame in focus_uniform_indexes:
            if len(selected_frames) >= target_total:
                break
            self._register_frame(selected_frames, frame, "事故过程补帧", frame_settings.min_frame_gap)

        if len(selected_frames) < target_total:
            full_uniform_indexes = self._build_uniform_frame_indexes(
                frame_count,
                max(frame_settings.uniform_frames * 2, target_total * 2),
            )
            for frame in full_uniform_indexes:
                if len(selected_frames) >= target_total:
                    break
                self._register_frame(selected_frames, frame, "全片补帧", frame_settings.min_frame_gap)

        frame_lookup = {
            int(item["frame"]): item for item in (yolo_summary.get("frame_summaries") or [])
        }
        manifest: list[dict[str, Any]] = []
        for frame in sorted(selected_frames):
            timestamp_seconds = round((frame - 1) / fps, 4)
            if frame in frame_lookup:
                timestamp_seconds = float(frame_lookup[frame].get("timestamp_seconds", timestamp_seconds))

            output_path = frames_dir / f"{filename_prefix}_{frame:06d}.jpg"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            self._extract_single_frame(
                video_path=video_path,
                frame_index=frame,
                output_path=output_path,
                source_width=width,
                source_frame_count=frame_count,
                source_duration_seconds=duration_seconds,
                timestamp_seconds=timestamp_seconds,
            )
            related_event_score = 0.0
            for event in event_candidates:
                if int(event.get("frame", 0) or 0) == frame:
                    related_event_score = float(event.get("event_score", 0.0) or 0.0)
                    break
            manifest.append(
                {
                    "frame": frame,
                    "timestamp_seconds": round(timestamp_seconds, 4),
                    "reason": selected_frames[frame],
                    "path": str(output_path.resolve()),
                    "source_name": source_name or video_path.name,
                    "media_type": media_type,
                    "category_id": category_id,
                    "category_label": category_label,
                    "category_subtitle": category_subtitle,
                    "category_sequence": category_sequence,
                    "sequence": sequence,
                    "group_sequence": frame,
                    "event_score": round(related_event_score, 4),
                }
            )
        return manifest

    def _extract_single_frame(
        self,
        video_path: Path,
        frame_index: int,
        output_path: Path,
        source_width: int,
        source_frame_count: int | None = None,
        source_duration_seconds: float | None = None,
        timestamp_seconds: float | None = None,
    ) -> None:
        ffmpeg_path = self._resolve_command(self.settings.input_generation.frames.ffmpeg_path)
        filter_parts = [f"select=eq(n\\,{max(frame_index - 1, 0)})"]
        max_side = self.settings.input_generation.frames.max_side
        if source_width > max_side:
            filter_parts.append(f"scale={max_side}:-2")

        command = [
            ffmpeg_path,
            "-y",
            "-i",
            str(video_path),
            "-vf",
            ",".join(filter_parts),
            "-frames:v",
            "1",
            "-threads",
            "1",
            "-q:v",
            str(self.settings.input_generation.frames.jpeg_quality),
            "-pix_fmt",
            "yuvj420p",
            "-strict",
            "unofficial",
            str(output_path),
        ]
        output_path.unlink(missing_ok=True)
        result = subprocess.run(
            command,
            cwd=str(self.settings.project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if result.returncode == 0 and output_path.exists():
            return

        primary_message = self._summarize_command_output(result.stderr or result.stdout or "")
        fallback_timestamp = self._resolve_frame_seek_timestamp(
            frame_index=frame_index,
            source_frame_count=source_frame_count,
            source_duration_seconds=source_duration_seconds,
            timestamp_seconds=timestamp_seconds,
        )
        if fallback_timestamp is not None:
            fallback_filters: list[str] = []
            if source_width > max_side:
                fallback_filters.append(f"scale={max_side}:-2")

            fallback_command = [
                ffmpeg_path,
                "-y",
                "-ss",
                f"{fallback_timestamp:.6f}",
                "-i",
                str(video_path),
            ]
            if fallback_filters:
                fallback_command.extend(["-vf", ",".join(fallback_filters)])
            fallback_command.extend(
                [
                    "-frames:v",
                    "1",
                    "-threads",
                    "1",
                    "-q:v",
                    str(self.settings.input_generation.frames.jpeg_quality),
                    "-pix_fmt",
                    "yuvj420p",
                    "-strict",
                    "unofficial",
                    str(output_path),
                ]
            )
            output_path.unlink(missing_ok=True)
            fallback_result = subprocess.run(
                fallback_command,
                cwd=str(self.settings.project_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            if fallback_result.returncode == 0 and output_path.exists():
                return

            fallback_message = self._summarize_command_output(
                fallback_result.stderr or fallback_result.stdout or ""
            )
            message = (
                f"主抽帧失败：{primary_message or '未知错误'}；"
                f"按时间点兜底失败：{fallback_message or '未知错误'}"
            )
            raise ProviderError(f"ffmpeg 抽帧失败（帧 {frame_index}）: {message}")

        raise ProviderError(f"ffmpeg 抽帧失败（帧 {frame_index}）: {primary_message or '未知错误'}")

    @staticmethod
    def _summarize_command_output(raw_output: str, max_lines: int = 12, max_chars: int = 1200) -> str:
        lines = [line.strip() for line in raw_output.splitlines() if line.strip()]
        if not lines:
            return ""
        message = "\n".join(lines[-max_lines:])
        if len(message) > max_chars:
            return message[-max_chars:]
        return message

    @staticmethod
    def _resolve_frame_seek_timestamp(
        frame_index: int,
        source_frame_count: int | None,
        source_duration_seconds: float | None,
        timestamp_seconds: float | None,
    ) -> float | None:
        duration = float(source_duration_seconds or 0.0)
        if duration <= 0:
            if timestamp_seconds is None:
                return None
            return max(float(timestamp_seconds), 0.0)

        if source_frame_count and source_frame_count > 1:
            clamped_frame = min(max(frame_index, 1), source_frame_count)
            ratio = (clamped_frame - 1) / max(source_frame_count - 1, 1)
            target = ratio * duration
        elif timestamp_seconds is not None:
            target = float(timestamp_seconds)
        else:
            return None

        max_seek = max(duration - 0.04, 0.0)
        return round(min(max(target, 0.0), max_seek), 6)

    def _render_generation_prompt(
        self,
        prompt_summary: dict[str, Any],
        frame_manifest: list[dict[str, Any]],
        grouped_materials_summary: dict[str, Any],
    ) -> str:
        template = self._load_generation_prompt()
        prompt_frame_manifest = [
            {
                "frame": item["frame"],
                "timestamp_seconds": item["timestamp_seconds"],
                "reason": item["reason"],
                "filename": Path(item["path"]).name,
                "source_name": item.get("source_name"),
                "media_type": item.get("media_type"),
                "category_id": item.get("category_id"),
                "category_label": item.get("category_label"),
                "category_subtitle": item.get("category_subtitle"),
                "category_sequence": item.get("category_sequence"),
                "sequence": item.get("sequence"),
            }
            for item in frame_manifest
        ]
        replacements = {
            GROUPED_MATERIALS_PLACEHOLDER: json.dumps(grouped_materials_summary, ensure_ascii=False, indent=2),
            YOLO_SUMMARY_PLACEHOLDER: json.dumps(prompt_summary, ensure_ascii=False, indent=2),
            FRAME_MANIFEST_PLACEHOLDER: json.dumps(prompt_frame_manifest, ensure_ascii=False, indent=2),
            INPUT_TEMPLATE_PLACEHOLDER: json.dumps(self._input_template, ensure_ascii=False, indent=2),
        }

        rendered = template
        for placeholder, value in replacements.items():
            if placeholder not in rendered:
                raise InputValidationError(f"事故信息生成提示词缺少占位内容: {placeholder}")
            rendered = rendered.replace(placeholder, value, 1)
        return rendered

    def _build_prompt_summary(self, yolo_summary: dict[str, Any]) -> dict[str, Any]:
        normalized_tracks = []
        for track in list(yolo_summary.get("track_summaries") or [])[
            : self.settings.input_generation.yolo.max_track_summaries
        ]:
            normalized_tracks.append(
                {
                    "track_id": int(track.get("track_id", 0) or 0),
                    "class_name": str(track.get("class_name") or "unknown"),
                    "first_frame": int(track.get("first_frame", 0) or 0),
                    "last_frame": int(track.get("last_frame", 0) or 0),
                    "sample_count": int(track.get("sample_count", 0) or 0),
                    "mean_speed_px_s": round(float(track.get("mean_speed_px_s", 0.0) or 0.0), 4),
                    "max_speed_px_s": round(float(track.get("max_speed_px_s", 0.0) or 0.0), 4),
                    "mean_abs_acceleration_px_s2": round(
                        float(track.get("mean_abs_acceleration_px_s2", 0.0) or 0.0),
                        4,
                    ),
                    "max_abs_acceleration_px_s2": round(
                        float(track.get("max_abs_acceleration_px_s2", 0.0) or 0.0),
                        4,
                    ),
                    "path_length_px": round(float(track.get("path_length_px", 0.0) or 0.0), 4),
                }
            )

        normalized_events = []
        for event in list(yolo_summary.get("event_candidates") or [])[
            : max(self.settings.input_generation.frames.event_frames * 2, 6)
        ]:
            normalized_events.append(
                {
                    "frame": int(event.get("frame", 0) or 0),
                    "timestamp_seconds": round(float(event.get("timestamp_seconds", 0.0) or 0.0), 4),
                    "event_score": round(float(event.get("event_score", 0.0) or 0.0), 4),
                    "reason": str(event.get("reason") or "关键事件候选"),
                    "object_count": int(event.get("object_count", 0) or 0),
                }
            )

        return {
            "source_type": "video",
            "video": yolo_summary.get("video", {}),
            "detection": yolo_summary.get("detection", {}),
            "track_summaries": normalized_tracks,
            "event_candidates": normalized_events,
        }

    def _build_image_prompt_summary(self, image_entries: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "source_type": "image",
            "video": {
                "image_count": len(image_entries),
            },
            "detection": {
                "note": "当前输入为静态图片，未执行 YOLO 轨迹、速度和加速度分析。",
            },
            "track_summaries": [],
            "event_candidates": [],
            "categories": [
                {
                    "category_id": str(item["category_id"]),
                    "category_label": str(item["category_label"]),
                    "category_subtitle": str(item.get("category_subtitle") or ""),
                    "category_sequence": int(item.get("category_sequence", 0) or 0),
                    "sequence": int(item.get("sequence", 0) or 0),
                    "source_name": str(item["original_name"]),
                }
                for item in image_entries
            ],
        }

    def _coerce_media_entries(
        self,
        image_paths: list[str] | None,
        video_paths: list[str] | None,
        media_entries: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if media_entries:
            return [dict(item) for item in media_entries]

        normalized_entries: list[dict[str, Any]] = []
        sequence = 1
        for index, raw_path in enumerate(image_paths or [], start=1):
            path = self.settings.resolve_path(raw_path)
            normalized_entries.append(
                {
                    "path": str(path.resolve()),
                    "original_name": path.name,
                    "media_type": "image",
                    "category_id": "default_images",
                    "category_label": "事故图片",
                    "category_subtitle": "",
                    "category_sequence": 1,
                    "group_sequence": index,
                    "sequence": sequence,
                }
            )
            sequence += 1
        for index, raw_path in enumerate(video_paths or [], start=1):
            path = self.settings.resolve_path(raw_path)
            normalized_entries.append(
                {
                    "path": str(path.resolve()),
                    "original_name": path.name,
                    "media_type": "video",
                    "category_id": "default_videos",
                    "category_label": "事故视频",
                    "category_subtitle": "",
                    "category_sequence": 2,
                    "group_sequence": index,
                    "sequence": sequence,
                }
            )
            sequence += 1
        return normalized_entries

    def _prepare_media_inputs(
        self,
        media_entries: list[dict[str, Any]],
        uploads_dir: Path,
    ) -> tuple[list[dict[str, Any]], list[Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        prepared_entries: list[dict[str, Any]] = []
        for entry in sorted(media_entries, key=lambda item: int(item.get("sequence", 0) or 0)):
            source_path = self.settings.resolve_path(str(entry.get("path") or ""))
            if not source_path.exists():
                raise InputValidationError(f"上传文件不存在: {source_path}")

            category_id = str(entry.get("category_id") or "").strip() or "uncategorized"
            category_label = str(entry.get("category_label") or "").strip() or "未分组材料"
            category_subtitle = str(entry.get("category_subtitle") or "").strip()
            category_sequence = int(entry.get("category_sequence", 0) or 0)
            media_type = str(entry.get("media_type") or "").strip()
            if media_type not in {"image", "video"}:
                raise InputValidationError(f"上传文件类型无效: {source_path.name}")

            group_sequence = int(entry.get("group_sequence", 0) or 0)
            global_sequence = int(entry.get("sequence", 0) or 0)
            category_dir = uploads_dir / category_id
            category_dir.mkdir(parents=True, exist_ok=True)
            suffix = source_path.suffix.lower() or (".jpg" if media_type == "image" else ".mp4")
            target_path = category_dir / f"{group_sequence:03d}_{source_path.stem}{suffix}"
            shutil.copy2(source_path, target_path)
            size_bytes = int(target_path.stat().st_size)

            prepared_entry = {
                "path": str(target_path.resolve()),
                "original_name": str(entry.get("original_name") or source_path.name),
                "stored_name": target_path.name,
                "media_type": media_type,
                "category_id": category_id,
                "category_label": category_label,
                "category_subtitle": category_subtitle,
                "category_sequence": category_sequence,
                "group_sequence": group_sequence,
                "sequence": global_sequence,
                "size_bytes": size_bytes,
            }
            prepared_entries.append(prepared_entry)

            bucket = grouped.setdefault(
                category_id,
                {
                    "category_id": category_id,
                    "category_label": category_label,
                    "category_subtitle": category_subtitle,
                    "sequence": category_sequence or len(grouped) + 1,
                    "image_count": 0,
                    "video_count": 0,
                    "total_bytes": 0,
                    "files": [],
                },
            )
            if media_type == "image":
                bucket["image_count"] += 1
            else:
                bucket["video_count"] += 1
            bucket["total_bytes"] += size_bytes
            bucket["files"].append(
                {
                    "original_name": prepared_entry["original_name"],
                    "stored_name": prepared_entry["stored_name"],
                    "media_type": media_type,
                    "size_bytes": size_bytes,
                    "sequence": group_sequence,
                    "global_sequence": global_sequence,
                    "path": prepared_entry["path"],
                }
            )

        summaries = [
            UploadGroupSummary.model_validate(grouped[key])
            for key in sorted(grouped, key=lambda item: int(grouped[item]["sequence"]))
        ]
        return prepared_entries, summaries

    def _build_image_frame_candidates(
        self,
        image_entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        manifest: list[dict[str, Any]] = []
        for entry in image_entries:
            manifest.append(
                {
                    "frame": int(entry["group_sequence"]),
                    "timestamp_seconds": 0.0,
                    "reason": "用户上传图片",
                    "path": str(entry["path"]),
                    "source_name": str(entry["original_name"]),
                    "media_type": "image",
                    "category_id": str(entry["category_id"]),
                    "category_label": str(entry["category_label"]),
                    "category_subtitle": str(entry.get("category_subtitle") or ""),
                    "category_sequence": int(entry.get("category_sequence", 0) or 0),
                    "sequence": int(entry["sequence"]),
                    "group_sequence": int(entry["group_sequence"]),
                    "event_score": 0.0,
                }
            )
        return manifest

    def _select_model_frame_manifest(
        self,
        image_entries: list[dict[str, Any]],
        key_frames: list[dict[str, Any]],
        upload_groups: list[Any],
    ) -> list[dict[str, Any]]:
        max_model_images = self.settings.input_generation.upload.max_model_images
        grouped_images: dict[str, list[dict[str, Any]]] = {}
        grouped_key_frames: dict[str, list[dict[str, Any]]] = {}
        for item in image_entries:
            grouped_images.setdefault(str(item["category_id"]), []).append(item)
        for item in key_frames:
            grouped_key_frames.setdefault(str(item["category_id"]), []).append(item)

        for items in grouped_images.values():
            items.sort(key=lambda item: (int(item["group_sequence"]), int(item["sequence"])))
        for items in grouped_key_frames.values():
            items.sort(
                key=lambda item: (
                    -float(item.get("event_score", 0.0) or 0.0),
                    float(item.get("timestamp_seconds", 0.0) or 0.0),
                    int(item["sequence"]),
                )
            )

        selected: list[dict[str, Any]] = []
        seen_paths: set[str] = set()

        def add_candidate(candidate: dict[str, Any] | None) -> None:
            if not candidate:
                return
            path = str(candidate["path"])
            if path in seen_paths or len(selected) >= max_model_images:
                return
            seen_paths.add(path)
            selected.append(candidate)

        group_order = [str(group.category_id) for group in sorted(upload_groups, key=lambda item: item.sequence)]

        for category_id in group_order:
            add_candidate((grouped_images.get(category_id) or [None])[0])
            if category_id not in grouped_images:
                add_candidate((grouped_key_frames.get(category_id) or [None])[0])

        for category_id in group_order:
            if category_id in grouped_key_frames:
                add_candidate((grouped_key_frames.get(category_id) or [None])[0])
            elif len(grouped_images.get(category_id) or []) > 1:
                add_candidate(grouped_images[category_id][1])

        ordered_candidates = sorted(
            image_entries + key_frames,
            key=lambda item: (
                int(item.get("category_sequence", 0) or 0),
                int(item["sequence"]),
                0 if str(item["media_type"]) == "image" else 1,
                int(item.get("group_sequence", 0) or 0),
                float(item.get("timestamp_seconds", 0.0) or 0.0),
            ),
        )
        for candidate in ordered_candidates:
            add_candidate(candidate)

        return selected[:max_model_images]

    def _normalize_group_definitions(
        self,
        group_definitions: list[dict[str, Any]] | None,
        prepared_entries: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized: dict[str, dict[str, Any]] = {}

        for index, raw_group in enumerate(group_definitions or [], start=1):
            if not isinstance(raw_group, dict):
                continue
            category_id = str(raw_group.get("category_id") or "").strip()
            category_label = str(raw_group.get("category_label") or "").strip()
            if not category_id or not category_label:
                continue
            normalized[category_id] = {
                "category_id": category_id,
                "category_label": category_label,
                "category_subtitle": str(raw_group.get("category_subtitle") or "").strip(),
                "sequence": int(raw_group.get("sequence", index) or index),
            }

        for index, entry in enumerate(prepared_entries, start=1):
            category_id = str(entry.get("category_id") or "").strip()
            category_label = str(entry.get("category_label") or "").strip()
            if not category_id or not category_label or category_id in normalized:
                continue
            normalized[category_id] = {
                "category_id": category_id,
                "category_label": category_label,
                "category_subtitle": str(entry.get("category_subtitle") or "").strip(),
                "sequence": int(entry.get("category_sequence", index) or index),
            }

        return sorted(
            normalized.values(),
            key=lambda item: (
                int(item.get("sequence", 0) or 0),
                str(item.get("category_id") or ""),
            ),
        )

    def _hydrate_upload_group_summaries(
        self,
        group_definitions: list[dict[str, Any]],
        upload_groups: list[UploadGroupSummary],
    ) -> list[UploadGroupSummary]:
        if not group_definitions:
            return upload_groups

        existing = {item.category_id: item for item in upload_groups}
        hydrated: list[UploadGroupSummary] = []
        for index, group in enumerate(group_definitions, start=1):
            category_id = str(group.get("category_id") or "").strip()
            if not category_id:
                continue
            current = existing.get(category_id)
            category_label = str(group.get("category_label") or "").strip() or (
                current.category_label if current else ""
            )
            category_subtitle = str(group.get("category_subtitle") or "").strip() or (
                current.category_subtitle if current else ""
            ) or ""
            hydrated.append(
                UploadGroupSummary(
                    category_id=category_id,
                    category_label=category_label,
                    category_subtitle=category_subtitle,
                    sequence=int(group.get("sequence", current.sequence if current else index) or index),
                    image_count=current.image_count if current else 0,
                    video_count=current.video_count if current else 0,
                    total_bytes=current.total_bytes if current else 0,
                    files=list(current.files) if current else [],
                )
            )
        return hydrated

    def _build_grouped_materials_prompt_summary(
        self,
        group_definitions: list[dict[str, Any]],
        prepared_entries: list[dict[str, Any]],
        frame_manifest: list[dict[str, Any]],
        key_frame_manifest: list[dict[str, Any]],
        combined_yolo_sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        original_media: dict[str, dict[str, list[dict[str, Any]]]] = {}
        selected_frames: dict[str, list[dict[str, Any]]] = {}
        extracted_key_frame_count: dict[str, int] = {}
        yolo_groups: dict[str, list[dict[str, Any]]] = {}

        for entry in sorted(
            prepared_entries,
            key=lambda item: (
                int(item.get("category_sequence", 0) or 0),
                int(item.get("group_sequence", 0) or 0),
                int(item.get("sequence", 0) or 0),
            ),
        ):
            category_id = str(entry.get("category_id") or "")
            bucket = original_media.setdefault(category_id, {"images": [], "videos": []})
            target_key = "videos" if str(entry.get("media_type") or "") == "video" else "images"
            bucket[target_key].append(
                {
                    "original_name": str(entry.get("original_name") or ""),
                    "stored_name": str(entry.get("stored_name") or ""),
                    "group_sequence": int(entry.get("group_sequence", 0) or 0),
                    "global_sequence": int(entry.get("sequence", 0) or 0),
                    "size_bytes": int(entry.get("size_bytes", 0) or 0),
                }
            )

        for item in key_frame_manifest:
            category_id = str(item.get("category_id") or "")
            extracted_key_frame_count[category_id] = extracted_key_frame_count.get(category_id, 0) + 1

        for item in sorted(
            frame_manifest,
            key=lambda entry: (
                int(entry.get("category_sequence", 0) or 0),
                int(entry.get("group_sequence", 0) or 0),
                int(entry.get("sequence", 0) or 0),
                float(entry.get("timestamp_seconds", 0.0) or 0.0),
            ),
        ):
            category_id = str(item.get("category_id") or "")
            selected_frames.setdefault(category_id, []).append(
                {
                    "source_name": str(item.get("source_name") or ""),
                    "media_type": str(item.get("media_type") or ""),
                    "group_sequence": int(item.get("group_sequence", 0) or 0),
                    "global_sequence": int(item.get("sequence", 0) or 0),
                    "frame": int(item.get("frame", 0) or 0),
                    "timestamp_seconds": round(float(item.get("timestamp_seconds", 0.0) or 0.0), 4),
                    "reason": str(item.get("reason") or ""),
                    "file_name": Path(str(item.get("path") or "")).name,
                }
            )

        for item in sorted(
            combined_yolo_sources,
            key=lambda entry: (
                int(entry.get("category_sequence", 0) or 0),
                int(entry.get("sequence", 0) or 0),
            ),
        ):
            category_id = str(item.get("category_id") or "")
            summary = dict(item.get("summary") or {})
            normalized_summary = self._build_prompt_summary(summary)
            yolo_groups.setdefault(category_id, []).append(
                {
                    "source_name": str(item.get("source_name") or "事故视频"),
                    "video": dict(normalized_summary.get("video") or {}),
                    "detection": dict(normalized_summary.get("detection") or {}),
                    "track_summaries": list(normalized_summary.get("track_summaries") or []),
                    "event_candidates": list(normalized_summary.get("event_candidates") or []),
                }
            )

        groups: list[dict[str, Any]] = []
        for index, group in enumerate(group_definitions, start=1):
            category_id = str(group.get("category_id") or "")
            originals = original_media.get(category_id, {"images": [], "videos": []})
            model_frames = selected_frames.get(category_id, [])
            yolo_summaries = yolo_groups.get(category_id, [])
            is_empty = not originals["images"] and not originals["videos"] and not model_frames and not yolo_summaries
            groups.append(
                {
                    "category_id": category_id,
                    "category_label": str(group.get("category_label") or ""),
                    "category_subtitle": str(group.get("category_subtitle") or ""),
                    "sequence": int(group.get("sequence", index) or index),
                    "is_empty": is_empty,
                    "original_materials": {
                        "images": originals["images"],
                        "videos": originals["videos"],
                    },
                    "selected_model_frames": model_frames,
                    "selected_model_frame_count": len(model_frames),
                    "extracted_key_frame_count": extracted_key_frame_count.get(category_id, 0),
                    "yolo_video_summaries": yolo_summaries,
                }
            )

        return {
            "group_count": len(groups),
            "non_empty_group_count": sum(1 for group in groups if not group["is_empty"]),
            "groups": groups,
        }

    def _build_generation_prompt_summary(
        self,
        prompt_sources: list[dict[str, Any]],
        has_images: bool,
        has_videos: bool,
    ) -> dict[str, Any]:
        ordered_sources = sorted(
            prompt_sources,
            key=lambda item: (
                int(item.get("category_sequence", 0) or 0),
                int(item.get("sequence", 0) or 0),
                0 if str(item.get("media_type") or "") == "image" else 1,
            ),
        )
        if len(prompt_sources) == 1 and has_videos and not has_images:
            source = dict(ordered_sources[0])
            source.pop("media_type", None)
            source.pop("source_name", None)
            return source

        if len(prompt_sources) == 1 and has_images and not has_videos:
            source = dict(ordered_sources[0])
            source.pop("media_type", None)
            source.pop("source_name", None)
            source.pop("file_names", None)
            return source

        return {
            "source_type": self._resolve_media_type(has_images, has_videos),
            "source_count": len(ordered_sources),
            "sources": ordered_sources,
        }

    def _build_generation_user_prompt(
        self,
        has_images: bool,
        has_videos: bool,
        has_existing_context: bool,
    ) -> str:
        media_label = "图片和关键帧" if has_images and has_videos else "关键帧" if has_videos else "图片"
        if has_existing_context:
            return (
                f"{media_label}已经按清单顺序附在消息里。"
                "请在现有事故信息草稿基础上补充、修正和统一，只输出最终 JSON 对象。"
            )
        return f"{media_label}已经按清单顺序附在消息里，请结合图像与摘要，只输出最终 JSON 对象。"

    def _build_image_captions(self, frame_manifest: list[dict[str, Any]]) -> list[str]:
        captions: list[str] = []
        for index, item in enumerate(frame_manifest):
            media_type = str(item.get("media_type") or "")
            source_name = str(item.get("source_name") or "")
            category_label = str(item.get("category_label") or "")
            if media_type == "image":
                captions.append(
                    f"图片 {index + 1}：分组 {category_label or '未分组材料'}，来源 {source_name or '用户上传图片'}，"
                    "请重点识别车辆、道路环境、碰撞位置与事故结果。"
                )
                continue
            captions.append(
                f"图片 {index + 1}：分组 {category_label or '未分组材料'}，来源 {source_name or '事故视频'}，"
                f"帧 {item['frame']}，时间 {item['timestamp_seconds']} 秒，抽帧原因：{item['reason']}。"
            )
        return captions

    def _augment_system_prompt_with_existing_data(
        self,
        system_prompt: str,
        existing_accident_data: dict[str, Any] | None,
    ) -> str:
        if not existing_accident_data:
            return system_prompt
        return (
            f"{system_prompt}\n\n"
            "以下是当前会话里已经确认或编辑过的事故信息草稿。"
            "本轮请在其基础上补充、修正、统一表述；若新证据与旧草稿冲突，应以更充分的新证据为准。\n"
            f"```json\n{json.dumps(existing_accident_data, ensure_ascii=False, indent=2)}\n```"
        )

    def _merge_existing_accident_data(
        self,
        existing_accident_data: dict[str, Any],
        generated_input: dict[str, Any],
    ) -> dict[str, Any]:
        return self._merge_template_nodes(existing_accident_data, generated_input)

    def _merge_template_nodes(self, existing_value: Any, generated_value: Any) -> Any:
        if isinstance(generated_value, dict) and isinstance(existing_value, dict):
            return {
                key: self._merge_template_nodes(existing_value.get(key), generated_value.get(key))
                for key in generated_value
            }

        generated_text = str(generated_value).strip() if generated_value is not None else ""
        existing_text = str(existing_value).strip() if existing_value is not None else ""
        return generated_text or existing_text

    def _ensure_total_video_duration_supported(self, total_video_seconds: float) -> None:
        max_video_seconds = self.settings.input_generation.frames.max_video_seconds
        if total_video_seconds > max_video_seconds:
            raise InputValidationError(
                f"本次上传视频总时长为 {total_video_seconds:.2f} 秒，超过当前后端支持上限 {max_video_seconds:.2f} 秒。"
            )

    def _allocate_video_frame_budgets(
        self,
        durations: list[float],
        remaining_budget: int,
    ) -> list[int]:
        if not durations:
            return []

        video_count = len(durations)
        if remaining_budget < video_count:
            raise InputValidationError("当前上传图片过多，已没有足够的模型图片预算处理视频。")

        max_frames_per_video = self.settings.input_generation.frames.max_frames
        minimum_per_video = 4 if remaining_budget >= video_count * 4 else 1
        budgets = [min(minimum_per_video, max_frames_per_video) for _ in durations]
        remaining = remaining_budget - sum(budgets)

        order = sorted(range(video_count), key=lambda index: durations[index], reverse=True)
        while remaining > 0:
            progressed = False
            for index in order:
                if budgets[index] >= max_frames_per_video:
                    continue
                budgets[index] += 1
                remaining -= 1
                progressed = True
                if remaining == 0:
                    break
            if not progressed:
                break
        return budgets

    def _write_combined_yolo_summary(
        self,
        yolo_root_dir: Path,
        has_images: bool,
        prompt_sources: list[dict[str, Any]],
        combined_yolo_sources: list[dict[str, Any]],
    ) -> Path | None:
        if not combined_yolo_sources:
            return None

        if len(combined_yolo_sources) == 1 and not has_images:
            raw_path = str(combined_yolo_sources[0].get("summary_path") or "").strip()
            if raw_path:
                return Path(raw_path).resolve()
            return None

        combined_path = yolo_root_dir / "combined_summary.json"
        self._write_json(
            combined_path,
            {
                "source_type": "mixed" if has_images else "video",
                "prompt_sources": prompt_sources,
                "video_sources": combined_yolo_sources,
            },
        )
        return combined_path.resolve()

    def _build_yolo_summary_preview(
        self,
        prompt_sources: list[dict[str, Any]],
        combined_yolo_sources: list[dict[str, Any]],
        has_images: bool,
    ) -> dict[str, Any] | None:
        if not combined_yolo_sources:
            return None

        preview_videos: list[dict[str, Any]] = []
        for item in combined_yolo_sources[:3]:
            summary = dict(item.get("summary") or {})
            video_meta = dict(summary.get("video") or {})
            detection = dict(summary.get("detection") or {})
            track_summaries = list(summary.get("track_summaries") or [])
            event_candidates = list(summary.get("event_candidates") or [])

            preview_videos.append(
                {
                    "source_name": str(item.get("source_name") or "事故视频"),
                    "category_id": str(item.get("category_id") or ""),
                    "category_label": str(item.get("category_label") or ""),
                    "category_subtitle": str(item.get("category_subtitle") or ""),
                    "category_sequence": int(item.get("category_sequence", 0) or 0),
                    "sequence": int(item.get("sequence", 0) or 0),
                    "duration_seconds": round(float(video_meta.get("duration_seconds", 0.0) or 0.0), 2),
                    "frame_count": int(video_meta.get("frame_count", 0) or 0),
                    "fps": round(float(video_meta.get("fps", 0.0) or 0.0), 2),
                    "unique_track_count": int(detection.get("unique_track_count", 0) or 0),
                    "total_detections": int(detection.get("total_detections", 0) or 0),
                    "class_counts": dict(detection.get("class_counts") or {}),
                    "track_highlights": [
                        {
                            "track_id": int(track.get("track_id", 0) or 0),
                            "class_name": str(track.get("class_name") or "unknown"),
                            "mean_speed_px_s": round(float(track.get("mean_speed_px_s", 0.0) or 0.0), 2),
                            "max_speed_px_s": round(float(track.get("max_speed_px_s", 0.0) or 0.0), 2),
                            "mean_abs_acceleration_px_s2": round(
                                float(track.get("mean_abs_acceleration_px_s2", 0.0) or 0.0),
                                2,
                            ),
                            "max_abs_acceleration_px_s2": round(
                                float(track.get("max_abs_acceleration_px_s2", 0.0) or 0.0),
                                2,
                            ),
                            "path_length_px": round(float(track.get("path_length_px", 0.0) or 0.0), 2),
                        }
                        for track in track_summaries[:3]
                    ],
                    "event_highlights": [
                        {
                            "frame": int(event.get("frame", 0) or 0),
                            "timestamp_seconds": round(
                                float(event.get("timestamp_seconds", 0.0) or 0.0),
                                2,
                            ),
                            "event_score": round(float(event.get("event_score", 0.0) or 0.0), 3),
                            "reason": str(event.get("reason") or "关键事件候选"),
                            "object_count": int(event.get("object_count", 0) or 0),
                        }
                        for event in event_candidates[:3]
                    ],
                }
            )

        return {
            "source_type": "mixed" if has_images else "video",
            "image_source_count": sum(
                int((item.get("video") or {}).get("image_count", 0) or 0)
                for item in prompt_sources
                if item.get("media_type") == "image"
            ),
            "video_source_count": len(combined_yolo_sources),
            "videos": sorted(
                preview_videos,
                key=lambda item: (
                    int(item.get("category_sequence", 0) or 0),
                    int(item.get("sequence", 0) or 0),
                ),
            ),
        }

    @staticmethod
    def _resolve_media_type(has_images: bool, has_videos: bool) -> str:
        if has_images and has_videos:
            return "mixed"
        if has_videos:
            return "video"
        return "image"

    def _normalize_generated_input(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._project_onto_template(self._input_template, payload)

    def _parse_vision_json(
        self,
        raw_response: str,
        system_prompt: str,
        image_paths: list[Path],
        image_captions: list[str],
    ) -> tuple[dict[str, Any], str]:
        try:
            return self._normalize_generated_input(extract_json_from_text(raw_response)), raw_response
        except InputValidationError as first_exc:
            logger.warning("视觉模型 JSON 解析失败，准备发起一次修复重试: %s", first_exc)

        repaired_raw = self.vision_provider.generate(
            system_prompt=system_prompt,
            user_prompt=(
                "你刚才的输出不是合法 JSON。"
                "请删除所有思考过程、解释、代码块和多余文字，"
                "严格按照给定模板字段，只重新输出一个可解析的 JSON 对象。"
            ),
            image_paths=image_paths,
            image_captions=image_captions,
        )
        return self._normalize_generated_input(extract_json_from_text(repaired_raw)), repaired_raw

    def _project_onto_template(self, template: Any, payload: Any) -> Any:
        if isinstance(template, dict):
            source = payload if isinstance(payload, dict) else {}
            return {
                key: self._project_onto_template(value, source.get(key))
                for key, value in template.items()
            }

        if payload is None:
            return template
        if isinstance(payload, str):
            return payload.strip()
        if isinstance(payload, (int, float, bool)):
            return str(payload)
        if isinstance(payload, list):
            return "；".join(str(item).strip() for item in payload if str(item).strip())
        return json.dumps(payload, ensure_ascii=False)

    def _backup_existing_input(self, target_path: Path) -> Path | None:
        if not target_path.exists():
            return None
        content = target_path.read_text(encoding="utf-8").strip()
        if not content:
            return None

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = self.settings.input_generation_backup_dir_path / f"input_accident-{timestamp}.json"
        shutil.copy2(target_path, backup_path)
        return backup_path

    def _load_generation_prompt(self) -> str:
        path = self.settings.input_generation_prompt_file
        if not path.exists():
            raise ConfigurationError(f"事故信息生成提示词不存在: {path}")
        return path.read_text(encoding="utf-8")

    def _load_input_template(self) -> dict[str, Any]:
        path = self.settings.input_generation_template_file
        if not path.exists():
            raise ConfigurationError(f"事故信息模板不存在: {path}")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ConfigurationError("事故信息模板必须是 JSON 对象。")
        return payload

    def _build_uniform_frame_indexes(self, frame_count: int, count: int) -> list[int]:
        if frame_count <= 0 or count <= 0:
            return []
        if frame_count <= count:
            return list(range(1, frame_count + 1))
        if count == 1:
            return [max(frame_count // 2, 1)]

        step = (frame_count - 1) / (count - 1)
        indexes = {int(round(1 + (step * i))) for i in range(count)}
        return sorted(max(index, 1) for index in indexes)

    def _build_uniform_frame_indexes_for_range(
        self,
        start_frame: int,
        end_frame: int,
        count: int,
    ) -> list[int]:
        if count <= 0:
            return []
        start = max(start_frame, 1)
        end = max(end_frame, start)
        frame_count = end - start + 1
        indexes = self._build_uniform_frame_indexes(frame_count, count)
        return [start + index - 1 for index in indexes]

    def _register_frame(
        self,
        selected_frames: dict[int, str],
        frame: int,
        reason: str,
        min_frame_gap: int,
    ) -> bool:
        if frame <= 0:
            return False
        if frame in selected_frames:
            return False
        if any(abs(existing - frame) < min_frame_gap for existing in selected_frames):
            return False
        selected_frames[frame] = reason
        return True

    def _probe_video_duration_seconds(self, video_path: Path) -> float:
        ffprobe_path = self._resolve_command(self.settings.input_generation.frames.ffprobe_path)
        command = [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=str(self.settings.project_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise InputValidationError(
                f"后端无法读取视频时长，请确认 ffprobe 可用后再上传：{video_path.name}"
            ) from exc

        if result.returncode != 0:
            message = (result.stderr or result.stdout or "").strip()
            raise InputValidationError(
                f"无法读取视频时长，请确认视频文件未损坏且格式受支持：{video_path.name}"
                + (f"（{message}）" if message else "")
            )

        try:
            duration_seconds = round(float((result.stdout or "").strip()), 4)
        except ValueError as exc:
            raise InputValidationError(
                f"视频时长读取结果无效，请更换标准 MP4/H.264 文件后重试：{video_path.name}"
            ) from exc

        if duration_seconds <= 0:
            raise InputValidationError(f"视频时长必须大于 0 秒：{video_path.name}")
        return duration_seconds

    def _ensure_video_duration_supported(self, video_path: Path, duration_seconds: float) -> None:
        max_video_seconds = self.settings.input_generation.frames.max_video_seconds
        if duration_seconds > max_video_seconds:
            raise InputValidationError(
                f"事故视频 {video_path.name} 时长为 {duration_seconds:.2f} 秒，超过当前后端支持上限 {max_video_seconds:.2f} 秒。"
            )

    def _resolve_target_frame_count(self, duration_seconds: float, frame_count: int) -> int:
        frame_settings = self.settings.input_generation.frames
        dynamic_target = frame_settings.base_frames + math.ceil(
            max(duration_seconds, 0.0) * frame_settings.frames_per_second
        )
        dynamic_target = max(dynamic_target, frame_settings.min_frames)
        dynamic_target = min(dynamic_target, frame_settings.max_frames, frame_count)
        return max(dynamic_target, 1)

    def _resolve_focus_window(
        self,
        frame_summaries: list[dict[str, Any]],
        frame_count: int,
        fps: float,
    ) -> tuple[int, int]:
        active_frames = [
            int(item.get("frame", 0))
            for item in frame_summaries
            if self._is_active_frame(item)
        ]
        if not active_frames:
            return 1, frame_count

        padding_frames = max(
            int(round(self.settings.input_generation.frames.active_window_padding_seconds * fps)),
            1,
        )
        start = max(min(active_frames) - padding_frames, 1)
        end = min(max(active_frames) + padding_frames, frame_count)
        return start, max(end, start)

    def _resolve_anchor_reason(self, index: int, total: int) -> str:
        if index == 0:
            return "事故前起始上下文"
        if index == total - 1:
            return "事故后结束上下文"
        if total >= 3 and index == total // 2:
            return "事故过程中心锚点"
        return "事故过程锚点"

    @staticmethod
    def _is_active_frame(frame_summary: dict[str, Any]) -> bool:
        if int(frame_summary.get("object_count", 0) or 0) > 0:
            return True
        if float(frame_summary.get("max_speed_px_s", 0.0) or 0.0) > 0:
            return True
        if float(frame_summary.get("max_abs_acceleration_px_s2", 0.0) or 0.0) > 0:
            return True
        return False

    def _prune_old_workspaces(self, current_workspace: Path) -> None:
        root = self.settings.input_generation_workspace_dir_path
        if not root.exists():
            return

        workspaces = sorted(
            (item for item in root.iterdir() if item.is_dir()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        keep_count = self.settings.input_generation.retain_workspace_count
        keep_paths = {item.resolve() for item in workspaces[:keep_count]}
        keep_paths.add(current_workspace.resolve())

        for workspace in workspaces:
            if workspace.resolve() in keep_paths:
                continue
            shutil.rmtree(workspace, ignore_errors=True)

    def _create_workspace_dir(self) -> Path:
        workspace_dir = self.settings.input_generation_workspace_dir_path / f"input-{uuid4().hex[:12]}"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir

    def _finalize_generation_artifact(
        self,
        workspace_dir: Path,
        media_type: str,
        generated_input: dict[str, Any],
        frame_manifest: list[dict[str, Any]],
        persist_generated_input: bool,
        frames_dir: Path | None,
        yolo_summary_path: Path | None,
        yolo_summary_preview: dict[str, Any] | None,
        upload_groups: list[Any],
        system_prompt: str,
        raw_response: str,
    ) -> InputGenerationArtifact:
        raw_response_path = None
        if self.settings.input_generation.retain_debug_artifacts:
            raw_response_path = workspace_dir / "vision_raw.txt"
            raw_response_path.write_text(raw_response, encoding="utf-8")
            prompt_path = workspace_dir / "vision_prompt.md"
            prompt_path.write_text(system_prompt, encoding="utf-8")

        manifest_path = workspace_dir / "frame_manifest.json"
        self._write_json(manifest_path, frame_manifest)

        generated_input_path = workspace_dir / "generated_input.json"
        self._write_json(generated_input_path, generated_input)

        backup_path = None
        target_input_path = generated_input_path
        if persist_generated_input:
            target_input_path = self.settings.input_generation_output_file
            backup_path = self._backup_existing_input(target_input_path)
            target_input_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_json(target_input_path, generated_input)

        self._prune_old_workspaces(current_workspace=workspace_dir)

        return InputGenerationArtifact(
            media_type=media_type,
            input_path=str(target_input_path.resolve()),
            generated_input=generated_input,
            backup_path=str(backup_path.resolve()) if backup_path else None,
            workspace_dir=str(workspace_dir.resolve()),
            yolo_summary_path=str(yolo_summary_path.resolve()) if yolo_summary_path else None,
            yolo_summary_preview=yolo_summary_preview,
            frames_dir=str(frames_dir.resolve()) if frames_dir else None,
            frame_manifest=frame_manifest,
            upload_groups=list(upload_groups),
            raw_response_path=str(raw_response_path.resolve()) if raw_response_path else None,
        )

    def _resolve_runtime_path(self, raw_path: str) -> Path:
        if "\\" in raw_path or "/" in raw_path or raw_path.startswith("."):
            return self.settings.resolve_path(raw_path)
        resolved = shutil.which(raw_path)
        if resolved:
            return Path(resolved)
        return Path(raw_path)

    def _resolve_command(self, raw_path: str) -> str:
        if "\\" in raw_path or "/" in raw_path or raw_path.startswith("."):
            return str(self.settings.resolve_path(raw_path))
        return raw_path

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
