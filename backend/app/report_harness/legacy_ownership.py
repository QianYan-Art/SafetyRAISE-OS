from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Request
from psycopg.types.json import Jsonb

from app.api import deps
from app.schemas.workflow import GenerateReportRequest
from app.services.auth_service import AuthenticatedUser


LEGACY_OWNERSHIP_FILENAME = ".legacy_report_ownership.json"


class LegacyOwnershipError(RuntimeError):
    def __init__(self, message: str, *, code: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class LegacyOwnershipRecord:
    owner_user_id: str | None
    owner_username: str | None
    trace_id: str
    session_id: str | None
    output_dir: str
    source: str


@dataclass(frozen=True, slots=True)
class LegacyOwnershipPersistence:
    session_id: str | None
    source: str | None


def assert_legacy_session_access(
    http_request: Request,
    session_id: str | None,
    current_user: AuthenticatedUser | None,
) -> None:
    """旧直调若显式引用会话，先阻止跨用户借用已归属会话。"""
    if not session_id or current_user is None:
        return

    database = _get_database_service(http_request)
    if database is None:
        return
    try:
        with database.connection() as conn:
            row = conn.execute(
                """
                select owner_user_id::text as owner_user_id, owner_username
                from chat_sessions
                where id=%s
                """,
                (str(session_id),),
            ).fetchone()
    except Exception as exc:
        raise LegacyOwnershipError(
            "报告会话归属暂时无法确认。",
            code="legacy_ownership_unavailable",
            status_code=503,
        ) from exc

    if row is None or not _row_owner_matches(row, current_user):
        raise LegacyOwnershipError(
            "当前账号无权使用该报告会话。",
            code="legacy_session_owner_required",
            status_code=404,
        )


def persist_legacy_report_ownership(
    http_request: Request,
    request: GenerateReportRequest,
    artifact: object,
    current_user: AuthenticatedUser | None,
    *,
    settings: object,
) -> LegacyOwnershipPersistence:
    """为旧直调产物写入持久会话归属，并原子写输出目录归属元数据。"""
    if current_user is None:
        return LegacyOwnershipPersistence(session_id=request.session_id, source=None)

    trace_id = str(getattr(artifact, "trace_id", "") or "").strip()
    if not trace_id:
        raise LegacyOwnershipError(
            "旧报告缺少可追踪的 trace_id，未提供导出。",
            code="legacy_export_owner_persistence_failed",
            status_code=503,
        )
    output_dir = _resolve_output_dir(settings, trace_id, getattr(artifact, "output_dir", None))
    report_result = _build_report_result(artifact, output_dir)
    requested_session_id = str(request.session_id or "").strip() or None
    database = _get_database_service(http_request)
    session_id: str | None = requested_session_id
    session_source: str | None = None

    if database is not None:
        try:
            session_state, session_id = _persist_session_result(
                database,
                session_id=requested_session_id,
                current_user=current_user,
                report_result=report_result,
                request=request,
            )
            if session_state in {"missing", "mismatch"}:
                raise LegacyOwnershipError(
                    "当前账号无权绑定该报告会话。",
                    code="legacy_session_owner_required",
                    status_code=404,
                )
            if session_state == "bound":
                session_source = "chat_sessions"
        except LegacyOwnershipError:
            raise
        except Exception as exc:
            raise LegacyOwnershipError(
                "旧报告归属无法持久化，未提供可追踪导出。",
                code="legacy_export_owner_persistence_failed",
                status_code=503,
            ) from exc

    sidecar_written = False
    if output_dir is not None:
        try:
            _write_ownership_sidecar(
                output_dir,
                {
                    "version": 1,
                    "trace_id": trace_id,
                    "owner_user_id": str(current_user.id),
                    "owner_username": str(current_user.username or ""),
                    "session_id": session_id,
                    "output_dir": str(output_dir),
                },
            )
            sidecar_written = True
        except OSError as exc:
            if session_source is None:
                raise LegacyOwnershipError(
                    "旧报告归属无法持久化，未提供可追踪导出。",
                    code="legacy_export_owner_persistence_failed",
                    status_code=503,
                ) from exc

    if session_source is None and not sidecar_written:
        raise LegacyOwnershipError(
            "旧报告归属无法持久化，未提供可追踪导出。",
            code="legacy_export_owner_persistence_failed",
            status_code=503,
        )
    return LegacyOwnershipPersistence(
        session_id=session_id,
        source=session_source or "output_sidecar",
    )


def is_legacy_export_authorized(
    http_request: Request,
    trace_id: str,
    current_user: AuthenticatedUser,
    *,
    settings: object,
) -> bool:
    """只接受持久会话或输出目录 sidecar 证明的归属。"""
    output_dir = _resolve_output_dir(settings, trace_id, None)
    if output_dir is None:
        return False

    sidecar = _read_ownership_sidecar(output_dir, trace_id)
    if sidecar is not None:
        return _record_owner_matches(sidecar, current_user)

    try:
        database = _get_database_service(http_request)
        if database is not None:
            record = _find_database_ownership(
                database,
                trace_id=trace_id,
                output_dir=output_dir,
            )
            return record is not None and _record_owner_matches(record, current_user)
    except Exception:
        # 没有可信 sidecar 时，数据库不可用必须拒绝，而不是退化为可下载。
        return False
    return False


def _get_database_service(
    http_request: Request,
) -> object | None:
    try:
        factory = http_request.app.dependency_overrides.get(
            deps.get_database_service,
            deps.get_database_service,
        )
        return factory()
    except Exception as exc:
        raise LegacyOwnershipError(
            "报告归属数据库暂时不可用。",
            code="legacy_ownership_unavailable",
            status_code=503,
        ) from exc


def _persist_session_result(
    database: object,
    *,
    session_id: str | None,
    current_user: AuthenticatedUser,
    report_result: dict[str, Any],
    request: GenerateReportRequest,
) -> tuple[str, str | None]:
    now = int(time.time() * 1000)
    with database.connection() as conn, conn.transaction():
        if session_id:
            row = conn.execute(
                """
                select id, owner_user_id::text as owner_user_id, owner_username
                from chat_sessions
                where id=%s
                for update
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return "missing", session_id
            if not _row_owner_matches(row, current_user):
                return "mismatch", session_id
            conn.execute(
                """
                update chat_sessions
                set report_result=%s::jsonb,
                    updated_at=%s,
                    session_state='report_ready'
                where id=%s
                """,
                (Jsonb(report_result), now, session_id),
            )
            return "bound", session_id

        created_session_id = f"legacy-report-{uuid4().hex}"
        accident_data = request.accident_data if isinstance(request.accident_data, dict) else {}
        source_type = "input_path" if request.input_path else "video_path" if request.video_path else "accident_data"
        source_name = Path(str(request.input_path or request.video_path or "legacy-report-api")).name
        conn.execute(
            """
            insert into chat_sessions (
                id, title, owner_user_id, owner_username, created_at, updated_at,
                source_type, source_name, messages, draft_json, draft_meta,
                report_result, session_state
            ) values (
                %s, %s, %s::uuid, %s, %s, %s,
                %s, %s, '[]'::jsonb, %s, %s::jsonb,
                %s::jsonb, 'report_ready'
            )
            """,
            (
                created_session_id,
                "交通事故分析报告",
                str(current_user.id),
                current_user.username,
                now,
                now,
                f"legacy_report_{source_type}",
                source_name,
                json.dumps(accident_data, ensure_ascii=False),
                Jsonb({
                    "legacy_report_api": True,
                    "legacy_source_type": source_type,
                    "legacy_source_name": source_name,
                }),
                Jsonb(report_result),
            ),
        )
        return "bound", created_session_id


def _build_report_result(artifact: object, output_dir: Path | None) -> dict[str, Any]:
    retrieval_meta = dict(getattr(artifact, "retrieval_meta", {}) or {})
    return {
        "trace_id": str(getattr(artifact, "trace_id", "") or ""),
        "status": "success",
        "output_dir": str(output_dir) if output_dir is not None else str(getattr(artifact, "output_dir", "") or ""),
        "guidance": _dump_json_value(getattr(artifact, "guidance", {})) or {},
        "report": _dump_json_value(getattr(artifact, "report", {})) or {},
        "input_generation": _dump_json_value(getattr(artifact, "input_generation", None)),
        "initial_knowledge_snippets": _dump_json_value(
            getattr(artifact, "initial_knowledge_snippets", [])
        ) or [],
        "knowledge_snippets": _dump_json_value(getattr(artifact, "knowledge_snippets", [])) or [],
        "retrieval_meta": retrieval_meta,
        "agentic_retrieval_rounds": _dump_json_value(
            getattr(artifact, "agentic_retrieval_rounds", [])
        ) or [],
    }


def _dump_json_value(value: Any) -> Any:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except TypeError:
            return model_dump()
    return value


def _resolve_output_dir(
    settings: object,
    trace_id: str,
    raw_output_dir: object,
) -> Path | None:
    root_value = getattr(settings, "output_dir_path", None)
    if root_value is None:
        return None
    try:
        root = Path(root_value).resolve()
        expected = (root / str(trace_id).strip()).resolve()
        if expected == root or not expected.is_relative_to(root):
            return None
        if raw_output_dir:
            candidate = Path(str(raw_output_dir))
            if not candidate.is_absolute():
                resolver = getattr(settings, "resolve_path", None)
                candidate = resolver(str(raw_output_dir)) if callable(resolver) else root / candidate
            if candidate.resolve() != expected:
                return None
        if not expected.exists() or not expected.is_dir():
            return None
        return expected
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _write_ownership_sidecar(output_dir: Path, payload: dict[str, Any]) -> None:
    target = output_dir / LEGACY_OWNERSHIP_FILENAME
    temporary = output_dir / f".{LEGACY_OWNERSHIP_FILENAME}.{uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_ownership_sidecar(output_dir: Path, trace_id: str) -> LegacyOwnershipRecord | None:
    try:
        payload = json.loads(
            (output_dir / LEGACY_OWNERSHIP_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if str(payload.get("trace_id") or "").strip() != str(trace_id).strip():
        return None
    declared_output_dir = str(payload.get("output_dir") or "").strip()
    if declared_output_dir != str(output_dir):
        return None
    owner_user_id = str(payload.get("owner_user_id") or "").strip() or None
    if owner_user_id is None:
        return None
    return LegacyOwnershipRecord(
        owner_user_id=owner_user_id,
        owner_username=str(payload.get("owner_username") or "").strip() or None,
        trace_id=str(trace_id),
        session_id=str(payload.get("session_id") or "").strip() or None,
        output_dir=declared_output_dir,
        source="output_sidecar",
    )


def _find_database_ownership(
    database: object,
    *,
    trace_id: str,
    output_dir: Path,
) -> LegacyOwnershipRecord | None:
    try:
        run_log = json.loads((output_dir / "run_log.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        run_log = None
    session_id = str((run_log or {}).get("session_id") or "").strip()
    if not session_id or str((run_log or {}).get("trace_id") or "").strip() != str(trace_id):
        return None

    with database.connection() as conn:
        row = conn.execute(
            """
            select id, owner_user_id::text as owner_user_id, owner_username
            from chat_sessions
            where id=%s
            """,
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    if str(_row_value(row, "id") or "").strip() != session_id:
        return None
    return _record_from_row(row, trace_id, output_dir, source="chat_session_run_log")


def _record_from_row(
    row: object,
    trace_id: str,
    output_dir: Path,
    *,
    source: str,
) -> LegacyOwnershipRecord:
    return LegacyOwnershipRecord(
        owner_user_id=str(_row_value(row, "owner_user_id") or "").strip() or None,
        owner_username=str(_row_value(row, "owner_username") or "").strip() or None,
        trace_id=str(trace_id),
        session_id=str(_row_value(row, "id") or "").strip() or None,
        output_dir=str(output_dir),
        source=source,
    )


def _row_owner_matches(row: object, current_user: AuthenticatedUser) -> bool:
    owner_user_id = str(_row_value(row, "owner_user_id") or "").strip()
    owner_username = str(_row_value(row, "owner_username") or "").strip()
    if owner_user_id:
        return owner_user_id == str(current_user.id)
    return bool(owner_username) and owner_username == str(current_user.username)


def _record_owner_matches(
    record: LegacyOwnershipRecord,
    current_user: AuthenticatedUser,
) -> bool:
    if record.owner_user_id:
        return record.owner_user_id == str(current_user.id)
    return bool(record.owner_username) and record.owner_username == str(current_user.username)


def _row_value(row: object, key: str) -> Any:
    if isinstance(row, dict):
        return row.get(key)
    if isinstance(row, (tuple, list)):
        indexes = {
            "id": 0,
            "owner_user_id": 1 if len(row) >= 3 else 0,
            "owner_username": 2 if len(row) >= 3 else 1,
            "report_result": 3,
        }
        index = indexes.get(key)
        if index is not None and index < len(row):
            return row[index]
    return None
