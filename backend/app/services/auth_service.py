from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from psycopg.errors import UniqueViolation

from app.core.exceptions import AuthenticationError, InputValidationError
from app.core.security import create_access_token, hash_password, verify_password
from app.core.settings import Settings
from app.schemas.auth import AuthTokenResponse, LoginRequest, RegisterRequest, UserSummaryResponse
from app.services.database_service import DatabaseService


@dataclass(slots=True)
class AuthenticatedUser:
    id: str
    username: str
    display_name: str | None
    role: Literal["admin", "user"]
    is_active: bool
    created_at: str
    updated_at: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthService:
    def __init__(self, settings: Settings, database_service: DatabaseService):
        self.settings = settings
        self.database_service = database_service
        self._ensure_bootstrap_admin()

    def register(self, request: RegisterRequest) -> AuthTokenResponse:
        password_hash = hash_password(request.password)
        with self.database_service.connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into users (username, password_hash, display_name, role)
                        values (%s, %s, %s, 'user')
                        returning id::text as id, username, display_name, role, is_active,
                                  created_at::text as created_at, updated_at::text as updated_at
                        """,
                        (request.username, password_hash, request.display_name),
                    )
                    row = cur.fetchone()
                conn.commit()
            except UniqueViolation as exc:
                conn.rollback()
                raise InputValidationError("用户名已存在，请更换后重试。") from exc
        user = self._coerce_user(row)
        token = create_access_token(
            auth_settings=self.settings.auth,
            user_id=user.id,
            username=user.username,
            role=user.role,
        )
        return AuthTokenResponse(access_token=token, user=self._to_response(user))

    def login(self, request: LoginRequest) -> AuthTokenResponse:
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id::text as id, username, password_hash, display_name, role, is_active,
                           created_at::text as created_at, updated_at::text as updated_at
                    from users
                    where username = %s
                    """,
                    (request.username,),
                )
                row = cur.fetchone()
        if not row or not verify_password(request.password, str(row.get("password_hash") or "")):
            raise AuthenticationError("用户名或密码错误。")
        user = self._coerce_user(row)
        if not user.is_active:
            raise AuthenticationError("当前账号已被停用。")
        token = create_access_token(
            auth_settings=self.settings.auth,
            user_id=user.id,
            username=user.username,
            role=user.role,
        )
        return AuthTokenResponse(access_token=token, user=self._to_response(user))

    def get_user_by_id(self, user_id: str) -> AuthenticatedUser:
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id::text as id, username, display_name, role, is_active,
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
                    "select id from users where username = %s",
                    (self.settings.auth.bootstrap_admin_username,),
                )
                row = cur.fetchone()
                if row:
                    return
                cur.execute(
                    """
                    insert into users (username, password_hash, display_name, role)
                    values (%s, %s, %s, 'admin')
                    """,
                    (
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
