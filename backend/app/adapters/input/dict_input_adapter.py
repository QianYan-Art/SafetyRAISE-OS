from typing import Any

from app.adapters.input.base import BaseInputAdapter
from app.core.exceptions import InputValidationError


class DictInputAdapter(BaseInputAdapter):
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload

    def load(self) -> dict[str, Any]:
        if not isinstance(self.payload, dict):
            raise InputValidationError("输入 payload 必须是字典对象。")
        if not self.payload:
            raise InputValidationError("输入 payload 不能为空。")
        return self.payload
