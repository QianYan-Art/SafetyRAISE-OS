import re
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


# 单级目录或文件名：ASCII 字母数字开头，只含 ._-；不含分隔符，也不可能是 . 或 ..。
_SAFE_PATH_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def is_safe_path_segment(value: object) -> bool:
    return isinstance(value, str) and _SAFE_PATH_SEGMENT.fullmatch(value) is not None


def is_path_within_any_root(path: Path, allowed_roots: list[Path]) -> bool:
    for root in allowed_roots:
        try:
            path.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False
