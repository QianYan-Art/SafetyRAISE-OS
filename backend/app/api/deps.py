from pathlib import Path

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import AuthenticationError
from app.core.security import decode_access_token
from app.core.settings import Settings, load_settings, resolve_config_path
from app.services.admin_service import AdminService
from app.services.auth_service import AuthService, AuthenticatedUser
from app.services.chat_session_service import ChatSessionService
from app.services.database_service import DatabaseService
from app.services.input_generation_service import InputGenerationService
from app.services.readiness_service import ReadinessService
from app.services.report_export_service import ReportExportService
from app.services.report_service import ReportService
from app.services.user_capability_config_service import UserCapabilityConfigService

_SETTINGS_CACHE: dict[str, object] = {
    "config_path": None,
    "mtime_ns": None,
    "settings": None,
}
_DATABASE_SERVICE_CACHE: dict[str, DatabaseService] = {}
_HTTP_BEARER = HTTPBearer(auto_error=False)


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


def get_database_service() -> DatabaseService:
    settings = get_settings()
    cache_key = settings.database.dsn
    cached = _DATABASE_SERVICE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    service = DatabaseService(settings=settings)
    _DATABASE_SERVICE_CACHE[cache_key] = service
    return service


def get_auth_service(
    database_service: DatabaseService = Depends(get_database_service),
) -> AuthService:
    return AuthService(settings=get_settings(), database_service=database_service)


def get_user_capability_config_service(
    database_service: DatabaseService = Depends(get_database_service),
) -> UserCapabilityConfigService:
    return UserCapabilityConfigService(settings=get_settings(), database_service=database_service)


def get_admin_service(
    database_service: DatabaseService = Depends(get_database_service),
) -> AdminService:
    return AdminService(settings=get_settings(), database_service=database_service)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_HTTP_BEARER),
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("请先登录后再继续操作。")
    payload = decode_access_token(credentials.credentials, get_settings().auth)
    user_id = str(payload.get("sub") or "").strip()
    if not user_id:
        raise AuthenticationError("登录状态无效，请重新登录。")
    return auth_service.get_user_by_id(user_id)


def get_optional_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_HTTP_BEARER),
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthenticatedUser | None:
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    payload = decode_access_token(credentials.credentials, get_settings().auth)
    user_id = str(payload.get("sub") or "").strip()
    if not user_id:
        raise AuthenticationError("登录状态无效，请重新登录。")
    return auth_service.get_user_by_id(user_id)


def require_admin_user(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    if not current_user.is_admin:
        raise AuthenticationError("当前账号无权访问管理员控制台。", status_code=403, code="PERMISSION_DENIED")
    return current_user


def get_authed_chat_session_service(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> ChatSessionService:
    return ChatSessionService(settings=get_settings(), current_user=current_user)


def _get_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
