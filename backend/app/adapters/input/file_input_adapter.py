import json
from pathlib import Path
from typing import Any

from app.adapters.input.base import BaseInputAdapter
from app.core.exceptions import InputValidationError


class FileInputAdapter(BaseInputAdapter):
    def __init__(self, file_path: str):
        self.file_path = Path(file_path).resolve()

    def load(self) -> dict[str, Any]:
        if not self.file_path.exists():
            raise InputValidationError(f"输入文件不存在: {self.file_path}")
        try:
            with self.file_path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except json.JSONDecodeError as exc:
            raise InputValidationError(f"输入文件不是合法 JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise InputValidationError("输入 JSON 顶层必须是对象。")
        return payload
