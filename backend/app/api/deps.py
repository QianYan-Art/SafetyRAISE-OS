from pathlib import Path

from app.core.settings import Settings, load_settings, resolve_config_path
from app.services.chat_session_service import ChatSessionService
from app.services.input_generation_service import InputGenerationService
from app.services.readiness_service import ReadinessService
from app.services.report_export_service import ReportExportService
from app.services.report_service import ReportService

_SETTINGS_CACHE: dict[str, object] = {
    "config_path": None,
    "mtime_ns": None,
    "settings": None,
}


def get_settings() -> Settings:
    config_path = resolve_config_path()
    mtime_ns = _get_mtime_ns(config_path)

    cached_settings = _SETTINGS_CACHE.get("settings")
    cached_path = _SETTINGS_CACHE.get("config_path")
    cached_mtime_ns = _SETTINGS_CACHE.get("mtime_ns")
    if (
        isinstance(cached_settings, Settings)
        and cached_path == config_path
        and cached_mtime_ns == mtime_ns
    ):
        return cached_settings

    settings = load_settings(str(config_path))
    _SETTINGS_CACHE["config_path"] = config_path
    _SETTINGS_CACHE["mtime_ns"] = mtime_ns
    _SETTINGS_CACHE["settings"] = settings
    return settings


def get_report_service() -> ReportService:
    return ReportService(settings=get_settings())


def get_input_generation_service() -> InputGenerationService:
    return ReportService(settings=get_settings())._build_input_generation_service()


def get_chat_session_service() -> ChatSessionService:
    return ChatSessionService(settings=get_settings())


def get_report_export_service() -> ReportExportService:
    return ReportExportService(settings=get_settings())


def get_readiness_service() -> ReadinessService:
    return ReadinessService(settings=get_settings())


def _get_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
