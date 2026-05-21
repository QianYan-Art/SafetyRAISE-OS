from pathlib import Path

from app.core.exceptions import InputValidationError
from app.core.settings import Settings


def resolve_api_path(
    settings: Settings,
    raw_path: str,
    *,
    field_name: str,
    allowed_roots: list[Path],
) -> Path:
    cleaned = str(raw_path or "").strip()
    if not cleaned:
        raise InputValidationError(f"{field_name} 不能为空。")

    resolved = settings.resolve_path(cleaned)
    if not is_path_within_any_root(resolved, allowed_roots):
        raise InputValidationError(
            f"{field_name} 不在允许的服务目录内，请重新选择已上传或已生成的文件。",
        )
    return resolved


def is_path_within_any_root(path: Path, allowed_roots: list[Path]) -> bool:
    for root in allowed_roots:
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False
