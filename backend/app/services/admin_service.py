from __future__ import annotations

from typing import Any

from psycopg.errors import UniqueViolation

from app.core.exceptions import InputValidationError, PermissionDeniedError, SessionNotFoundError
from app.core.security import hash_password
from app.core.settings import Settings
from app.schemas.admin import (
    AdminCreateUserRequest,
    AdminSpaceRecord,
    AdminUpdateSpaceRequest,
    AdminUpdateUserRequest,
    AdminUserResponse,
)
from app.schemas.chat_session import UpdateChatSessionRequest
from app.services.auth_service import AuthenticatedUser
from app.services.chat_session_service import ChatSessionService
from app.services.database_service import DatabaseService


class AdminService:
    def __init__(self, settings: Settings, database_service: DatabaseService):
        self.settings = settings
        self.database_service = database_service

    def list_users(self) -> list[AdminUserResponse]:
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id::text as id, username, display_name, role, is_active,
                           created_at::text as created_at, updated_at::text as updated_at
                    from users
                    order by created_at asc
                    """
                )
                rows = list(cur.fetchall())
        return [self._to_user_response(row) for row in rows]

    def create_user(self, request: AdminCreateUserRequest) -> AdminUserResponse:
        with self.database_service.connection() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        insert into users (username, password_hash, display_name, role, is_active)
                        values (%s, %s, %s, %s, %s)
                        returning id::text as id, username, display_name, role, is_active,
                                  created_at::text as created_at, updated_at::text as updated_at
                        """,
                        (
                            request.username,
                            hash_password(request.password),
                            request.display_name,
                            request.role,
                            request.is_active,
                        ),
                    )
                    row = cur.fetchone()
                conn.commit()
            except UniqueViolation as exc:
                conn.rollback()
                raise InputValidationError("用户名已存在，请更换后重试。") from exc
        return self._to_user_response(row)

    def update_user(self, user_id: str, request: AdminUpdateUserRequest) -> AdminUserResponse:
        updates: dict[str, Any] = request.model_dump(exclude_unset=True)
        assignments: list[str] = []
        values: list[Any] = []
        if "display_name" in updates:
            assignments.append("display_name = %s")
            values.append(updates["display_name"])
        if "role" in updates:
            assignments.append("role = %s")
            values.append(updates["role"])
        if "is_active" in updates:
            assignments.append("is_active = %s")
            values.append(updates["is_active"])
        if "password" in updates and updates["password"]:
            assignments.append("password_hash = %s")
            values.append(hash_password(str(updates["password"])))
        if not assignments:
            raise InputValidationError("没有可更新的用户字段。")
        values.append(user_id)
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    update users
                    set {", ".join(assignments)}, updated_at = now()
                    where id = %s
                    returning id::text as id, username, display_name, role, is_active,
                              created_at::text as created_at, updated_at::text as updated_at
                    """,
                    values,
                )
                row = cur.fetchone()
            conn.commit()
        if row is None:
            raise InputValidationError("目标用户不存在。")
        return self._to_user_response(row)

    def delete_user(self, user_id: str, current_user: AuthenticatedUser) -> None:
        owned_session_ids: list[str] = []
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select username from users where id = %s",
                    (user_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise InputValidationError("目标用户不存在。")
                username = str(row["username"])
                if username == self.settings.auth.bootstrap_admin_username:
                    raise PermissionDeniedError("不能删除默认管理员账号。")
                if user_id == current_user.id:
                    raise PermissionDeniedError("不能删除当前登录中的管理员账号。")
                cur.execute(
                    "select id from chat_sessions where owner_user_id = %s order by updated_at desc, created_at desc",
                    (user_id,),
                )
                owned_session_ids = [str(item["id"]) for item in cur.fetchall()]
        service = ChatSessionService(settings=self.settings)
        for session_id in owned_session_ids:
            try:
                service.delete_session(session_id)
            except SessionNotFoundError:
                continue
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("delete from users where id = %s", (user_id,))
            conn.commit()

    def list_spaces(self, *, exclude_owner_user_id: str | None = None, exclude_owner_username: str | None = None) -> list[AdminSpaceRecord]:
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                params: list[str] = []
                owner_filter = ""
                if exclude_owner_user_id or exclude_owner_username:
                    owner_filter = """
                    where (%s = '' or owner_user_id is null or owner_user_id::text <> %s)
                      and (%s = '' or owner_username is null or owner_username <> %s)
                    """
                    params = [
                        exclude_owner_user_id or "",
                        exclude_owner_user_id or "",
                        exclude_owner_username or "",
                        exclude_owner_username or "",
                    ]
                cur.execute(
                    f"""
                    select
                        id as session_id,
                        owner_user_id::text as owner_user_id,
                        owner_username,
                        title,
                        created_at,
                        updated_at,
                        session_state,
                        source_type,
                        source_name,
                        jsonb_array_length(messages) as message_count,
                        (
                            case
                                when coalesce(jsonb_array_length(report_result -> 'knowledge_snippets'), 0) > 0
                                  or coalesce(jsonb_array_length(report_result -> 'initial_knowledge_snippets'), 0) > 0
                                then 1 else 0
                            end
                            +
                            case
                                when coalesce(jsonb_array_length(report_result -> 'agentic_retrieval_rounds'), 0) > 0
                                then 1 else 0
                            end
                            +
                            case
                                when coalesce(draft_meta ->> 'yolo_summary_path', '') <> ''
                                  or coalesce((report_result -> 'input_generation') ->> 'yolo_summary_path', '') <> ''
                                then 1 else 0
                            end
                            +
                            case
                                when coalesce(nullif(draft_json, ''), '') <> ''
                                  or draft_meta ? 'generated_input'
                                  or report_result ? 'input_generation'
                                then 1 else 0
                            end
                            +
                            case
                                when coalesce(draft_meta ->> 'frames_dir', '') <> ''
                                  or coalesce((report_result -> 'input_generation') ->> 'frames_dir', '') <> ''
                                then 1 else 0
                            end
                        ) as linked_artifact_count
                    from chat_sessions
                    {owner_filter}
                    order by updated_at desc, created_at desc
                    """,
                    params,
                )
                rows = list(cur.fetchall())
        return [
            AdminSpaceRecord(
                session_id=str(row["session_id"]),
                owner_user_id=row.get("owner_user_id"),
                owner_username=self._mask_username(row.get("owner_username")),
                title=self._redact_space_title(
                    session_id=str(row["session_id"]),
                    raw_title=row.get("title"),
                    created_at=int(row["created_at"]),
                ),
                created_at=int(row["created_at"]),
                updated_at=int(row["updated_at"]),
                session_state=str(row["session_state"]),
                source_type=row.get("source_type"),
                source_name=None,
                message_count=int(row.get("message_count") or 0),
                linked_artifact_count=int(row.get("linked_artifact_count") or 0),
                redacted=True,
            )
            for row in rows
        ]

    def update_space(self, session_id: str, request: AdminUpdateSpaceRequest) -> AdminSpaceRecord:
        updates = request.model_dump(exclude_unset=True)
        owner_user_id = updates.get("owner_user_id")
        if "owner_user_id" in updates:
            normalized_owner_user_id = str(owner_user_id or "").strip()
            if normalized_owner_user_id:
                with self.database_service.connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            select id::text as id, username
                            from users
                            where id = %s and is_active = true
                            """,
                            (normalized_owner_user_id,),
                        )
                        owner_row = cur.fetchone()
                if owner_row is None:
                    raise InputValidationError("目标归属用户不存在或已停用。")
                updates["owner_user_id"] = str(owner_row["id"])
                updates["owner_username"] = str(owner_row["username"])
            else:
                updates["owner_user_id"] = None
                updates["owner_username"] = None
        service = ChatSessionService(settings=self.settings)
        record = service.update_session(
            session_id,
            UpdateChatSessionRequest(**updates),
        )
        return AdminSpaceRecord(
            session_id=record.id,
            owner_user_id=record.owner_user_id,
            owner_username=self._mask_username(record.owner_username),
            title=self._redact_space_title(
                session_id=record.id,
                raw_title=record.title,
                created_at=record.created_at,
            ),
            created_at=record.created_at,
            updated_at=record.updated_at,
            session_state=record.session_state,
            source_type=record.source_type,
            source_name=None,
            message_count=len(record.messages),
            linked_artifact_count=len(record.linked_artifacts),
            redacted=True,
        )

    def delete_space(self, session_id: str) -> None:
        service = ChatSessionService(settings=self.settings)
        try:
            service.delete_session(session_id)
        except SessionNotFoundError as exc:
            raise InputValidationError(str(exc)) from exc

    def cleanup_orphan_spaces(self) -> int:
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id
                    from chat_sessions
                    where owner_user_id is null
                    order by updated_at desc, created_at desc
                    """
                )
                session_ids = [str(row["id"]) for row in cur.fetchall()]
        service = ChatSessionService(settings=self.settings)
        deleted_count = 0
        for session_id in session_ids:
            try:
                service.delete_session(session_id)
            except SessionNotFoundError:
                continue
            deleted_count += 1
        return deleted_count

    @staticmethod
    def _to_user_response(row: dict[str, Any]) -> AdminUserResponse:
        return AdminUserResponse(
            id=str(row["id"]),
            username=str(row["username"]),
            display_name=row.get("display_name"),
            role=str(row["role"]),
            is_active=bool(row["is_active"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _mask_username(username: Any) -> str | None:
        normalized = str(username or "").strip()
        if not normalized:
            return None
        if len(normalized) <= 2:
            return normalized[0] + "*"
        if len(normalized) <= 6:
            return f"{normalized[0]}***{normalized[-1]}"
        return f"{normalized[:2]}***{normalized[-2:]}"

    @staticmethod
    def _redact_space_title(*, session_id: str, raw_title: Any, created_at: int) -> str:
        suffix = session_id[-6:] if session_id else "space"
        base = str(raw_title or "").strip()
        if base and base.isdigit() and len(base) >= 8:
            return base[-5:]
        return f"#{suffix}"
