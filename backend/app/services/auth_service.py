from __future__ import annotations

import hmac
import logging
from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from psycopg.errors import UniqueViolation

from app.core.auth_throttle import bounded_password_hash_slot, get_auth_rate_limiter
from app.core.exceptions import AuthenticationError, InputValidationError, PermissionDeniedError
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password_constant_time,
)
from app.core.settings import Settings
from app.schemas.auth import (
    AuthTokenResponse,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    UserSummaryResponse,
)
from app.services.database_service import DatabaseService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class AuthenticatedUser:
    id: str
    username: str
    display_name: str | None
    role: Literal["admin", "user"]
    is_active: bool
    created_at: str
    updated_at: str
    token_version: int = 1

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthService:
    def __init__(self, settings: Settings, database_service: DatabaseService):
        self.settings = settings
        self.database_service = database_service
        self._ensure_bootstrap_admin()

    def register(self, request: RegisterRequest, *, client_ip: str | None = None) -> AuthTokenResponse:
        auth_cfg = self.settings.auth
        if not auth_cfg.allow_self_registration:
            raise PermissionDeniedError("当前站点已关闭自助注册，请联系管理员创建账号。")
        expected_invite = (auth_cfg.registration_invite_code or "").strip()
        if expected_invite:
            provided_invite = (request.invite_code or "").strip()
            if not provided_invite or not hmac.compare_digest(provided_invite, expected_invite):
                raise PermissionDeniedError("注册邀请码无效。")

        if auth_cfg.login_rate_limit_enabled:
            limiter = get_auth_rate_limiter()
            limiter.check_and_record(
                bucket_key=f"register:ip:{client_ip or 'unknown'}",
                limit=auth_cfg.register_max_attempts_per_ip,
                window_seconds=auth_cfg.login_window_seconds,
            )

        if request.username.lower() == auth_cfg.bootstrap_admin_username.strip().lower():
            raise InputValidationError("该用户名为系统保留名称，请更换后重试。")

        with bounded_password_hash_slot(max_concurrency=auth_cfg.password_hash_max_concurrency):
            password_hash = hash_password(request.password)

        with self.database_service.connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into users (id, username, password_hash, display_name, role)
                        values (%s, %s, %s, %s, 'user')
                        returning id::text as id, username, display_name, role, is_active,
                                  token_version,
                                  created_at::text as created_at, updated_at::text as updated_at
                        """,
                        (str(uuid4()), request.username, password_hash, request.display_name),
                    )
                    row = cur.fetchone()
                conn.commit()
            except UniqueViolation as exc:
                conn.rollback()
                raise InputValidationError("无法完成注册，请更换用户名或检查输入后重试。") from exc
        user = self._coerce_user(row)
        logger.info("用户注册成功 | user_id=%s | username=%s | ip=%s", user.id, user.username, client_ip or "-")
        token = create_access_token(
            auth_settings=self.settings.auth,
            user_id=user.id,
            username=user.username,
            role=user.role,
            token_version=user.token_version,
        )
        return AuthTokenResponse(access_token=token, user=self._to_response(user))

    def login(self, request: LoginRequest, *, client_ip: str | None = None) -> AuthTokenResponse:
        auth_cfg = self.settings.auth
        ip_key = client_ip or "unknown"
        account_key = request.username.lower()
        if auth_cfg.login_rate_limit_enabled:
            limiter = get_auth_rate_limiter()
            limiter.check_and_record(
                bucket_key=f"login:ip:{ip_key}",
                limit=auth_cfg.login_max_attempts_per_ip,
                window_seconds=auth_cfg.login_window_seconds,
            )
            limiter.check_and_record(
                bucket_key=f"login:account:{ip_key}:{account_key}",
                limit=auth_cfg.login_max_attempts_per_account,
                window_seconds=auth_cfg.login_window_seconds,
            )

        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id::text as id, username, password_hash, display_name, role, is_active,
                           token_version,
                           created_at::text as created_at, updated_at::text as updated_at
                    from users
                    where username = %s
                    """,
                    (request.username,),
                )
                row = cur.fetchone()

        stored_hash = str(row.get("password_hash") or "") if row else None
        with bounded_password_hash_slot(max_concurrency=auth_cfg.password_hash_max_concurrency):
            password_ok = verify_password_constant_time(request.password, stored_hash)

        if not row or not password_ok:
            logger.warning("登录失败 | username=%s | ip=%s", request.username, ip_key)
            raise AuthenticationError("用户名或密码错误。")
        user = self._coerce_user(row)
        if not user.is_active:
            logger.warning("已停用账号尝试登录 | user_id=%s | username=%s | ip=%s", user.id, user.username, ip_key)
            raise AuthenticationError("当前账号已被停用。")
        if auth_cfg.login_rate_limit_enabled:
            get_auth_rate_limiter().clear_bucket(f"login:account:{ip_key}:{account_key}")
        token = create_access_token(
            auth_settings=self.settings.auth,
            user_id=user.id,
            username=user.username,
            role=user.role,
            token_version=user.token_version,
        )
        return AuthTokenResponse(access_token=token, user=self._to_response(user))

    def logout(self, user_id: str) -> None:
        """递增 token_version，立即吊销该用户所有既有访问令牌。"""
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update users
                    set token_version = token_version + 1, updated_at = now()
                    where id = %s
                    """,
                    (user_id,),
                )
            conn.commit()
        logger.info("用户登出并吊销令牌 | user_id=%s", user_id)

    def change_password(self, user_id: str, request: ChangePasswordRequest) -> AuthTokenResponse:
        """验证当前口令后更新密码，并递增 token_version 使旧令牌全部失效。"""
        auth_cfg = self.settings.auth
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id::text as id, username, password_hash, display_name, role, is_active,
                           token_version,
                           created_at::text as created_at, updated_at::text as updated_at
                    from users
                    where id = %s
                    for update
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise AuthenticationError("当前登录账号不存在。")
                with bounded_password_hash_slot(max_concurrency=auth_cfg.password_hash_max_concurrency):
                    if not verify_password_constant_time(request.current_password, str(row.get("password_hash") or "")):
                        raise AuthenticationError("当前密码不正确。")
                    new_hash = hash_password(request.new_password)
                cur.execute(
                    """
                    update users
                    set password_hash = %s,
                        token_version = token_version + 1,
                        updated_at = now()
                    where id = %s
                    returning id::text as id, username, display_name, role, is_active,
                              token_version,
                              created_at::text as created_at, updated_at::text as updated_at
                    """,
                    (new_hash, user_id),
                )
                updated_row = cur.fetchone()
            conn.commit()
        user = self._coerce_user(updated_row)
        logger.info("用户自助修改密码并轮换令牌版本 | user_id=%s | username=%s", user.id, user.username)
        token = create_access_token(
            auth_settings=self.settings.auth,
            user_id=user.id,
            username=user.username,
            role=user.role,
            token_version=user.token_version,
        )
        return AuthTokenResponse(access_token=token, user=self._to_response(user))

    def get_user_by_id(self, user_id: str) -> AuthenticatedUser:
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id::text as id, username, display_name, role, is_active,
                           token_version,
                           created_at::text as created_at, updated_at::text as updated_at
                    from users
                    where id = %s
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
        if not row:
            raise AuthenticationError("当前登录账号不存在。")
        user = self._coerce_user(row)
        if not user.is_active:
            raise AuthenticationError("当前账号已被停用。")
        return user

    def _ensure_bootstrap_admin(self) -> None:
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select id, role from users where username = %s",
                    (self.settings.auth.bootstrap_admin_username,),
                )
                row = cur.fetchone()
                if row:
                    existing_role = row.get("role") if isinstance(row, dict) else None
                    if existing_role and existing_role != "admin":
                        logger.error(
                            "引导管理员用户名 %s 已被非管理员账号占用（role=%s），请核查数据库。",
                            self.settings.auth.bootstrap_admin_username,
                            existing_role,
                        )
                    return
                if not self.settings.auth.bootstrap_admin_password:
                    # 开发环境允许不设初始口令；生产 profile 已在启动检查中拒绝这种配置。
                    logger.warning("未设置 BOOTSTRAP_ADMIN_PASSWORD，跳过创建引导管理员 %s。",
                                   self.settings.auth.bootstrap_admin_username)
                    return
                cur.execute(
                    """
                    insert into users (id, username, password_hash, display_name, role)
                    values (%s, %s, %s, %s, 'admin')
                    on conflict (username) do nothing
                    """,
                    (
                        str(uuid4()),
                        self.settings.auth.bootstrap_admin_username,
                        hash_password(self.settings.auth.bootstrap_admin_password),
                        self.settings.auth.bootstrap_admin_display_name,
                    ),
                )
            conn.commit()

    @staticmethod
    def _coerce_user(row: dict) -> AuthenticatedUser:
        return AuthenticatedUser(
            id=str(row["id"]),
            username=str(row["username"]),
            display_name=row.get("display_name"),
            role=str(row["role"]),
            is_active=bool(row["is_active"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            token_version=int(row.get("token_version") or 1),
        )

    @staticmethod
    def _to_response(user: AuthenticatedUser) -> UserSummaryResponse:
        return UserSummaryResponse(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            is_active=user.is_active,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )
