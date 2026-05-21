import json
import mimetypes
import shutil
from collections import Counter
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.deps import get_input_generation_service
from app.core.exceptions import (
    InputValidationError,
    UnsupportedMediaError,
    UploadLimitExceededError,
)
from app.core.path_guard import resolve_api_path
from app.schemas.workflow import (
    GenerateInputFromUploadResponse,
    GenerateInputFromVideoRequest,
    GenerateInputFromVideoResponse,
)
from app.services.input_generation_service import InputGenerationService

router = APIRouter(prefix="/api/v1/inputs", tags=["inputs"])
UPLOAD_CHUNK_SIZE = 1024 * 1024


@router.post("/generate-from-video", response_model=GenerateInputFromVideoResponse)
def generate_input_from_video(
    request: GenerateInputFromVideoRequest,
    service: InputGenerationService = Depends(get_input_generation_service),
):
    try:
        artifact = service.generate(
            video_path=str(
                resolve_api_path(
                    service.settings,
                    request.video_path,
                    field_name="video_path",
                    allowed_roots=[
                        service.settings.backend_data_dir_path,
                        service.settings.input_generation_workspace_dir_path,
                        service.settings.resolve_path("backend/data/runtime/uploads"),
                    ],
                )
            ),
            persist_generated_input=request.persist_generated_input,
        )
    finally:
        _close_service(service)

    return GenerateInputFromVideoResponse(
        status="success",
        media_type=artifact.media_type,
        input_path=artifact.input_path,
        generated_input=artifact.generated_input,
        backup_path=artifact.backup_path,
        workspace_dir=artifact.workspace_dir,
        yolo_summary_path=artifact.yolo_summary_path,
        yolo_summary_preview=artifact.yolo_summary_preview,
        frames_dir=artifact.frames_dir,
        frame_manifest=artifact.frame_manifest,
        raw_response_path=artifact.raw_response_path,
    )


@router.post("/generate-from-upload", response_model=GenerateInputFromUploadResponse)
async def generate_input_from_upload(
    files: list[UploadFile] | None = File(default=None),
    file: UploadFile | None = File(default=None),
    upload_manifest: str | None = Form(default=None),
    service: InputGenerationService = Depends(get_input_generation_service),
):
    upload_files = list(files or [])
    if file is not None:
        upload_files.append(file)
    if not upload_files:
        raise InputValidationError("至少需要上传一个图片或视频文件。")

    upload_settings = service.settings.input_generation.upload
    if len(upload_files) > upload_settings.max_files:
        raise UploadLimitExceededError(
            f"单次最多上传 {upload_settings.max_files} 个文件。",
        )

    parsed_manifest = _parse_upload_manifest(upload_manifest, expected_count=len(upload_files))
    _validate_upload_manifest(parsed_manifest, upload_settings)
    upload_dir = service.settings.resolve_path("backend/data/runtime/uploads") / f"upload-{uuid4().hex[:12]}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    original_names: list[str] = []
    media_entries: list[dict] = []
    total_bytes = 0

    try:
        for index, upload in enumerate(upload_files, start=1):
            manifest_item = parsed_manifest["items"][index - 1]
            original_name = Path(upload.filename or "upload.bin").name
            suffix = Path(original_name).suffix or _guess_suffix(upload.content_type)
            stored_name = f"source_{index:03d}{suffix}"
            media_type = _detect_media_type(upload.content_type, stored_name)
            declared_media_type = str(manifest_item.get("media_type") or "").strip()
            if declared_media_type and declared_media_type != media_type:
                raise InputValidationError(f"上传清单中的文件类型与实际文件不一致：{original_name}")
            max_file_bytes = (
                upload_settings.max_video_bytes
                if media_type == "video"
                else upload_settings.max_image_bytes
            )
            stored_path = upload_dir / stored_name
            total_bytes = await _write_upload_to_path(
                upload=upload,
                stored_path=stored_path,
                total_bytes=total_bytes,
                max_total_bytes=upload_settings.max_total_bytes,
                max_file_bytes=max_file_bytes,
                original_name=original_name,
                media_type=media_type,
            )
            original_names.append(original_name)
            media_entries.append(
                {
                    "path": str(stored_path),
                    "original_name": original_name,
                    "media_type": media_type,
                    "category_id": manifest_item["category_id"],
                    "category_label": manifest_item["category_label"],
                    "category_subtitle": manifest_item.get("category_subtitle") or "",
                    "category_sequence": manifest_item["category_sequence"],
                    "group_sequence": manifest_item["group_sequence"],
                    "sequence": manifest_item["sequence"],
                }
            )

        artifact = service.generate_from_media(
            media_entries=media_entries,
            group_definitions=parsed_manifest.get("groups"),
            persist_generated_input=False,
        )
    finally:
        for upload in upload_files:
            await upload.close()
        shutil.rmtree(upload_dir, ignore_errors=True)
        _close_service(service)

    return GenerateInputFromUploadResponse(
        status="success",
        media_type=artifact.media_type,
        file_name=original_names[0] if len(original_names) == 1 else None,
        file_names=original_names,
        source_count=len(original_names),
        input_path=artifact.input_path,
        generated_input=artifact.generated_input,
        backup_path=artifact.backup_path,
        workspace_dir=artifact.workspace_dir,
        yolo_summary_path=artifact.yolo_summary_path,
        yolo_summary_preview=artifact.yolo_summary_preview,
        frames_dir=artifact.frames_dir,
        frame_manifest=artifact.frame_manifest,
        upload_groups=[item.model_dump() for item in artifact.upload_groups],
        raw_response_path=artifact.raw_response_path,
    )


def _detect_media_type(content_type: str | None, stored_path: str | Path) -> str:
    normalized = (content_type or "").lower()
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("image/"):
        return "image"

    guessed_type, _ = mimetypes.guess_type(Path(stored_path).name)
    if guessed_type:
        if guessed_type.startswith("video/"):
            return "video"
        if guessed_type.startswith("image/"):
            return "image"
    raise UnsupportedMediaError("无法识别上传文件类型，请上传图片或视频。")


def _guess_suffix(content_type: str | None) -> str:
    guessed = mimetypes.guess_extension((content_type or "").lower()) or ""
    return guessed


def _parse_upload_manifest(raw_payload: str | None, *, expected_count: int) -> dict:
    if not raw_payload or not raw_payload.strip():
        raise InputValidationError("upload_manifest 不能为空。")
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise InputValidationError("upload_manifest 不是合法 JSON。") from exc
    if not isinstance(payload, dict):
        raise InputValidationError("upload_manifest 必须是 JSON 对象。")

    raw_groups = payload.get("groups")
    raw_items = payload.get("items")
    if not isinstance(raw_groups, list) or not isinstance(raw_items, list):
        raise InputValidationError("upload_manifest 必须同时包含 groups 和 items 数组。")
    if len(raw_items) != expected_count:
        raise InputValidationError("upload_manifest.items 数量与实际上传文件数量不一致。")

    groups: dict[str, dict] = {}
    for index, raw_group in enumerate(raw_groups, start=1):
        if not isinstance(raw_group, dict):
            raise InputValidationError("upload_manifest.groups 中存在非法分组。")
        category_id = str(raw_group.get("category_id") or "").strip()
        category_label = str(raw_group.get("category_label") or "").strip()
        if not category_id or not category_label:
            raise InputValidationError("upload_manifest.groups 中的分组必须包含 category_id 和 category_label。")
        groups[category_id] = {
            "category_id": category_id,
            "category_label": category_label,
            "category_subtitle": str(raw_group.get("category_subtitle") or "").strip(),
            "sequence": int(raw_group.get("sequence", index) or index),
        }

    items: list[dict] = []
    for index, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, dict):
            raise InputValidationError("upload_manifest.items 中存在非法条目。")
        category_id = str(raw_item.get("category_id") or "").strip()
        media_type = str(raw_item.get("media_type") or "").strip()
        original_name = str(raw_item.get("original_name") or "").strip()
        if category_id not in groups:
            raise InputValidationError("upload_manifest.items 中存在未知分组。")
        if media_type not in {"image", "video"}:
            raise InputValidationError("upload_manifest.items.media_type 仅支持 image 或 video。")
        if not original_name:
            raise InputValidationError("upload_manifest.items.original_name 不能为空。")
        group = groups[category_id]
        items.append(
            {
                "category_id": category_id,
                "category_label": group["category_label"],
                "category_subtitle": group["category_subtitle"],
                "category_sequence": group["sequence"],
                "sequence": int(raw_item.get("sequence", index) or index),
                "group_sequence": int(raw_item.get("group_sequence", index) or index),
                "media_type": media_type,
                "original_name": original_name,
            }
        )

    return {
        "groups": list(groups.values()),
        "items": sorted(items, key=lambda item: int(item["sequence"])),
    }


def _validate_upload_manifest(parsed_manifest: dict, upload_settings) -> None:  # noqa: ANN001
    items = list(parsed_manifest.get("items") or [])
    if not items:
        raise InputValidationError("upload_manifest.items 不能为空。")

    total_images = sum(1 for item in items if item["media_type"] == "image")
    total_videos = sum(1 for item in items if item["media_type"] == "video")
    if total_images > upload_settings.max_total_images:
        raise UploadLimitExceededError(
            f"本次上传图片总数不能超过 {upload_settings.max_total_images} 张。"
        )
    if total_videos > upload_settings.max_total_videos:
        raise UploadLimitExceededError(
            f"本次上传视频总数不能超过 {upload_settings.max_total_videos} 个。"
        )

    image_counter: Counter[str] = Counter()
    video_counter: Counter[str] = Counter()
    for item in items:
        if item["media_type"] == "image":
            image_counter[str(item["category_id"])] += 1
        else:
            video_counter[str(item["category_id"])] += 1

    for category_id, count in image_counter.items():
        if count > upload_settings.max_images_per_group:
            raise UploadLimitExceededError(
                f"分组 {category_id} 的图片数量不能超过 {upload_settings.max_images_per_group} 张。"
            )
    for category_id, count in video_counter.items():
        if count > upload_settings.max_videos_per_group:
            raise UploadLimitExceededError(
                f"分组 {category_id} 的视频数量不能超过 {upload_settings.max_videos_per_group} 个。"
            )


async def _write_upload_to_path(
    upload: UploadFile,
    stored_path: Path,
    total_bytes: int,
    max_total_bytes: int,
    max_file_bytes: int,
    original_name: str,
    media_type: str,
) -> int:
    file_bytes = 0
    media_label = "视频" if media_type == "video" else "图片"
    with stored_path.open("wb") as handle:
        while True:
            chunk = await upload.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            total_bytes += len(chunk)
            file_bytes += len(chunk)
            if total_bytes > max_total_bytes:
                raise UploadLimitExceededError(
                    f"单次上传总大小不能超过 {max_total_bytes // (1024 * 1024)} MB。",
                )
            if file_bytes > max_file_bytes:
                raise UploadLimitExceededError(
                    f"{media_label}文件 {original_name} 大小不能超过 {max_file_bytes // (1024 * 1024)} MB。",
                )
            handle.write(chunk)
    return total_bytes


def _close_service(service: object) -> None:
    close = getattr(service, "close", None)
    if callable(close):
        close()
