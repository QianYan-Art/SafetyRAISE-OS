import argparse
import csv
import json
import math
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import cv2
from ultralytics import YOLO


def probe_video_duration_seconds(video_path: Path) -> float | None:
    command = [
        "ffprobe",
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
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None

    if result.returncode != 0:
        return None

    try:
        duration_seconds = float((result.stdout or "").strip())
    except ValueError:
        return None

    if duration_seconds <= 0:
        return None
    return round(duration_seconds, 4)


def read_video_meta(video_path: Path) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"无法打开视频: {video_path}")

    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    capture.release()

    if fps <= 0:
        fps = 10.0

    duration_seconds = probe_video_duration_seconds(video_path)
    if duration_seconds is None and frame_count > 0 and fps > 0:
        duration_seconds = round(frame_count / fps, 4)

    return {
        "path": str(video_path.resolve()),
        "fps": fps,
        "frame_count": frame_count,
        "width": width,
        "height": height,
        "duration_seconds": duration_seconds or 0.0,
    }


def finalize_video_meta(meta: dict[str, Any], observed_frame_count: int) -> tuple[dict[str, Any], float]:
    normalized_meta = dict(meta)
    fps = float(normalized_meta.get("fps", 0.0) or 0.0)
    duration_seconds = float(normalized_meta.get("duration_seconds", 0.0) or 0.0)

    if observed_frame_count > 0:
        normalized_meta["frame_count"] = observed_frame_count
        if duration_seconds > 0:
            fps = observed_frame_count / duration_seconds
        elif fps > 0:
            duration_seconds = observed_frame_count / fps

    if fps <= 0:
        fps = 10.0

    if duration_seconds <= 0 and observed_frame_count > 0:
        duration_seconds = observed_frame_count / fps

    normalized_meta["fps"] = round(fps, 4)
    normalized_meta["duration_seconds"] = round(duration_seconds, 4) if duration_seconds > 0 else 0.0
    return normalized_meta, fps


def apply_frame_timestamps(
    detection_rows: list[dict[str, Any]],
    frame_summaries: list[dict[str, Any]],
    fps: float,
) -> None:
    if fps <= 0:
        fps = 10.0

    for row in detection_rows:
        row["timestamp_seconds"] = round((int(row["frame"]) - 1) / fps, 4)

    for item in frame_summaries:
        item["timestamp_seconds"] = round((int(item["frame"]) - 1) / fps, 4)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="提取事故视频的 YOLO 轨迹和动态特征")
    parser.add_argument("--video", required=True, help="输入视频路径")
    parser.add_argument("--output-dir", required=True, help="输出目录")
    parser.add_argument("--model", required=True, help="YOLO 权重路径")
    parser.add_argument("--conf", type=float, default=0.3, help="检测置信度阈值")
    parser.add_argument("--tracker", default="bytetrack.yaml", help="跟踪器配置")
    parser.add_argument("--device", default=None, help="推理设备，如 cpu 或 0")
    parser.add_argument(
        "--classes",
        nargs="+",
        default=["person", "bicycle", "car", "motorcycle", "bus", "truck"],
        help="保留的类别名称列表",
    )
    parser.add_argument("--max-track-summaries", type=int, default=12, help="轨迹摘要保留数量")
    return parser


def extract_features(args: argparse.Namespace) -> dict[str, Any]:
    video_path = Path(args.video).resolve()
    output_dir = Path(args.output_dir).resolve()
    model_path = Path(args.model).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not video_path.exists():
        raise FileNotFoundError(f"视频不存在: {video_path}")
    if not model_path.exists():
        raise FileNotFoundError(f"YOLO 权重不存在: {model_path}")

    meta = read_video_meta(video_path)
    fps = float(meta["fps"])

    model = YOLO(str(model_path))
    names = model.names
    relevant_class_ids = sorted(
        int(class_id)
        for class_id, class_name in names.items()
        if str(class_name) in set(args.classes)
    )

    detection_rows: list[dict[str, Any]] = []
    frame_rows: dict[int, list[dict[str, Any]]] = defaultdict(list)
    frame_summaries: list[dict[str, Any]] = []
    class_counter: Counter[str] = Counter()

    results = model.track(
        source=str(video_path),
        stream=True,
        persist=True,
        tracker=args.tracker,
        conf=args.conf,
        classes=relevant_class_ids,
        verbose=False,
        device=args.device,
    )

    for frame_index, result in enumerate(results, start=1):
        boxes = result.boxes
        timestamp_seconds = 0.0
        current_rows: list[dict[str, Any]] = []

        if boxes is not None:
            for box in boxes:
                if box.id is None:
                    continue
                track_id = int(box.id.tolist()[0])
                class_id = int(box.cls.tolist()[0])
                class_name = str(result.names[class_id])
                x1, y1, x2, y2 = map(float, box.xyxy.tolist()[0])
                width = max(x2 - x1, 0.0)
                height = max(y2 - y1, 0.0)
                center_x = round((x1 + x2) / 2, 4)
                center_y = round((y1 + y2) / 2, 4)

                row = {
                    "frame": frame_index,
                    "timestamp_seconds": timestamp_seconds,
                    "track_id": track_id,
                    "class_id": class_id,
                    "class_name": class_name,
                    "confidence": round(float(box.conf.tolist()[0]), 4),
                    "x1": round(x1, 4),
                    "y1": round(y1, 4),
                    "x2": round(x2, 4),
                    "y2": round(y2, 4),
                    "center_x": center_x,
                    "center_y": center_y,
                    "width": round(width, 4),
                    "height": round(height, 4),
                    "area": round(width * height, 4),
                    "speed_px_s": 0.0,
                    "acceleration_px_s2": 0.0,
                }
                detection_rows.append(row)
                current_rows.append(row)
                class_counter[class_name] += 1

        frame_rows[frame_index] = current_rows
        frame_summaries.append(
            {
                "frame": frame_index,
                "timestamp_seconds": timestamp_seconds,
                "object_count": len(current_rows),
                "min_pair_distance_px": round(compute_min_pair_distance(current_rows), 4),
                "max_speed_px_s": 0.0,
                "max_abs_acceleration_px_s2": 0.0,
            }
        )

    meta, fps = finalize_video_meta(meta, len(frame_summaries))
    apply_frame_timestamps(detection_rows, frame_summaries, fps)
    apply_motion_metrics(detection_rows, frame_summaries, fps)
    track_summaries = build_track_summaries(detection_rows, args.max_track_summaries)
    event_candidates = build_event_candidates(frame_summaries)

    detections_csv = output_dir / "detections.csv"
    write_csv(detections_csv, detection_rows)

    track_summary_csv = output_dir / "track_summaries.csv"
    write_csv(track_summary_csv, track_summaries)

    frame_summary_csv = output_dir / "frame_summaries.csv"
    write_csv(frame_summary_csv, frame_summaries)

    summary = {
        "video": meta,
        "detection": {
            "relevant_classes": list(args.classes),
            "total_detections": len(detection_rows),
            "unique_track_count": len({row["track_id"] for row in detection_rows}),
            "class_counts": dict(class_counter),
        },
        "track_summaries": track_summaries,
        "frame_summaries": frame_summaries,
        "event_candidates": event_candidates,
        "artifacts": {
            "detections_csv": str(detections_csv),
            "track_summaries_csv": str(track_summary_csv),
            "frame_summaries_csv": str(frame_summary_csv),
        },
    }

    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def apply_motion_metrics(
    detection_rows: list[dict[str, Any]],
    frame_summaries: list[dict[str, Any]],
    fps: float,
) -> None:
    rows_by_track: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in detection_rows:
        rows_by_track[int(row["track_id"])].append(row)

    frame_lookup = {int(item["frame"]): item for item in frame_summaries}
    for track_rows in rows_by_track.values():
        track_rows.sort(key=lambda item: int(item["frame"]))
        previous_row: dict[str, Any] | None = None
        previous_speed = 0.0
        for row in track_rows:
            if previous_row is None:
                previous_row = row
                continue

            delta_frames = max(int(row["frame"]) - int(previous_row["frame"]), 1)
            delta_seconds = delta_frames / fps
            dx = float(row["center_x"]) - float(previous_row["center_x"])
            dy = float(row["center_y"]) - float(previous_row["center_y"])
            speed = math.sqrt(dx * dx + dy * dy) / delta_seconds
            acceleration = (speed - previous_speed) / delta_seconds

            row["speed_px_s"] = round(speed, 4)
            row["acceleration_px_s2"] = round(acceleration, 4)
            previous_speed = speed
            previous_row = row

            frame_summary = frame_lookup.get(int(row["frame"]))
            if frame_summary is None:
                continue
            frame_summary["max_speed_px_s"] = round(
                max(float(frame_summary["max_speed_px_s"]), speed),
                4,
            )
            frame_summary["max_abs_acceleration_px_s2"] = round(
                max(float(frame_summary["max_abs_acceleration_px_s2"]), abs(acceleration)),
                4,
            )


def build_track_summaries(
    detection_rows: list[dict[str, Any]],
    max_track_summaries: int,
) -> list[dict[str, Any]]:
    rows_by_track: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in detection_rows:
        rows_by_track[int(row["track_id"])].append(row)

    track_summaries: list[dict[str, Any]] = []
    for track_id, track_rows in rows_by_track.items():
        sorted_rows = sorted(track_rows, key=lambda item: int(item["frame"]))
        speeds = [float(row["speed_px_s"]) for row in sorted_rows]
        accelerations = [abs(float(row["acceleration_px_s2"])) for row in sorted_rows]
        widths = [float(row["width"]) for row in sorted_rows]
        heights = [float(row["height"]) for row in sorted_rows]
        path_length = 0.0
        previous_row: dict[str, Any] | None = None
        for row in sorted_rows:
            if previous_row is not None:
                dx = float(row["center_x"]) - float(previous_row["center_x"])
                dy = float(row["center_y"]) - float(previous_row["center_y"])
                path_length += math.sqrt(dx * dx + dy * dy)
            previous_row = row

        track_summaries.append(
            {
                "track_id": track_id,
                "class_name": sorted_rows[0]["class_name"],
                "class_id": sorted_rows[0]["class_id"],
                "first_frame": sorted_rows[0]["frame"],
                "last_frame": sorted_rows[-1]["frame"],
                "first_time_seconds": sorted_rows[0]["timestamp_seconds"],
                "last_time_seconds": sorted_rows[-1]["timestamp_seconds"],
                "sample_count": len(sorted_rows),
                "mean_speed_px_s": round(sum(speeds) / len(speeds), 4) if speeds else 0.0,
                "max_speed_px_s": round(max(speeds), 4) if speeds else 0.0,
                "mean_abs_acceleration_px_s2": round(sum(accelerations) / len(accelerations), 4)
                if accelerations
                else 0.0,
                "max_abs_acceleration_px_s2": round(max(accelerations), 4) if accelerations else 0.0,
                "path_length_px": round(path_length, 4),
                "mean_width_px": round(sum(widths) / len(widths), 4) if widths else 0.0,
                "mean_height_px": round(sum(heights) / len(heights), 4) if heights else 0.0,
            }
        )

    track_summaries.sort(
        key=lambda item: (
            -float(item["max_abs_acceleration_px_s2"]),
            -float(item["max_speed_px_s"]),
            -float(item["path_length_px"]),
            item["track_id"],
        )
    )
    return track_summaries[:max_track_summaries]


def build_event_candidates(frame_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    valid_distances = [
        float(item["min_pair_distance_px"])
        for item in frame_summaries
        if float(item["min_pair_distance_px"]) > 0
    ]
    min_distance = min(valid_distances) if valid_distances else 0.0
    max_acc = max((float(item["max_abs_acceleration_px_s2"]) for item in frame_summaries), default=0.0)
    max_speed = max((float(item["max_speed_px_s"]) for item in frame_summaries), default=0.0)

    scored: list[dict[str, Any]] = []
    for item in frame_summaries:
        distance = float(item["min_pair_distance_px"])
        speed_score = float(item["max_speed_px_s"]) / max_speed if max_speed > 0 else 0.0
        acc_score = (
            float(item["max_abs_acceleration_px_s2"]) / max_acc
            if max_acc > 0
            else 0.0
        )
        distance_score = 0.0
        if min_distance > 0 and distance > 0:
            distance_score = min_distance / distance

        score = round((acc_score * 0.5) + (speed_score * 0.3) + (distance_score * 0.2), 4)
        reason_parts = []
        if float(item["max_abs_acceleration_px_s2"]) > 0:
            reason_parts.append("加速度峰值")
        if float(item["max_speed_px_s"]) > 0:
            reason_parts.append("速度峰值")
        if distance > 0:
            reason_parts.append("目标距离接近")

        scored.append(
            {
                **item,
                "event_score": score,
                "reason": "、".join(reason_parts) or "均匀补帧",
            }
        )

    scored.sort(
        key=lambda item: (
            -float(item["event_score"]),
            -float(item["max_abs_acceleration_px_s2"]),
            -float(item["max_speed_px_s"]),
            item["frame"],
        )
    )
    return scored[:20]


def compute_min_pair_distance(rows: list[dict[str, Any]]) -> float:
    if len(rows) < 2:
        return 0.0

    min_distance = 0.0
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            dx = float(left["center_x"]) - float(right["center_x"])
            dy = float(left["center_y"]) - float(right["center_y"])
            distance = math.sqrt(dx * dx + dy * dy)
            if min_distance == 0.0 or distance < min_distance:
                min_distance = distance
    return min_distance


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    output_dir = Path(args.output_dir).resolve()
    extract_features(args)
    print(
        json.dumps(
            {
                "status": "ok",
                "summary_path": str((output_dir / "summary.json").resolve()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
