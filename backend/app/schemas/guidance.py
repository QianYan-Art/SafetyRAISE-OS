from typing import Any

from pydantic import BaseModel


class GuidanceResult(BaseModel):
    raw_text: str
    parsed: dict[str, Any]
