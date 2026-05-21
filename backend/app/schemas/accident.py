from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class AccidentRecord(BaseModel):
    data: dict[str, Any]
    source: str = "file"
    ingest_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
