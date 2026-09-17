from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from psycopg.rows import dict_row

from app.api.deps import get_current_user, get_report_export_service
from app.api.routes_report_runs import HarnessRoute, get_report_run_service
from app.report_harness.errors import HarnessError
from app.report_harness.exports import render_run_export
from app.report_harness.release_registry import export_eligibility
from app.services.auth_service import AuthenticatedUser
from app.services.report_run_service import ReportRunService

router = APIRouter(prefix="/api/v1/report-runs", tags=["report-runs"], route_class=HarnessRoute)


@router.get("")
def list_runs(session_id: str = Query(min_length=1, max_length=200),
              limit: int = Query(20, ge=1, le=100), cursor: UUID | None = None,
              user: AuthenticatedUser = Depends(get_current_user),
              service: ReportRunService = Depends(get_report_run_service)):
    with service.store.connection() as conn, conn.transaction():
        conn.row_factory = dict_row
        service.store._owned_session(conn, user.id, session_id)
        anchor_time = None
        if cursor is not None:
            anchor = conn.execute(
                "SELECT created_at FROM report_runs WHERE run_id=%s AND owner_user_id=%s "
                "AND session_id=%s AND deleted_at IS NULL", (cursor, user.id, session_id),
            ).fetchone()
            if anchor is None:
                raise HarnessError("invalid_cursor", 422)
            anchor_time = anchor["created_at"]
        rows = conn.execute(
            "SELECT run_id FROM report_runs WHERE owner_user_id=%s AND session_id=%s "
            "AND deleted_at IS NULL AND (%s::uuid IS NULL "
            "OR (created_at,run_id)<(%s::timestamptz,%s::uuid)) "
            "ORDER BY created_at DESC,run_id DESC LIMIT %s",
            (user.id, session_id, cursor, anchor_time, cursor, limit + 1),
        ).fetchall()
    # 公共视图再次验证所有权及删除屏障，不从原始 document 输出私有上下文。
    return {
        "runs": [service.get(user.id, str(row["run_id"])) for row in rows[:limit]],
        "next_cursor": str(rows[limit - 1]["run_id"]) if len(rows) > limit else None,
    }


@router.get("/{run_id}/exports/{export_format}")
def download_run_export(
    run_id: UUID, export_format: Literal["md", "docx", "pdf"],
    mode: Literal["formal", "engineering"] = "formal",
    user: AuthenticatedUser = Depends(get_current_user),
    service: ReportRunService = Depends(get_report_run_service),
    renderer=Depends(get_report_export_service),
):
    record = service.store.get(user.id, str(run_id))
    registry = service.dependencies.release_registry
    content, media_type, filename = render_run_export(
        record, export_format, mode=mode, registry=registry, renderer=renderer,
        force_engineering=service.dependencies.force_engineering_exports,
    )
    # 渲染之后再验证屏障和当前批准绑定，撤销或删除不能借缓存下载。
    current = service.store.get(user.id, str(run_id))
    if mode == "formal" and not export_eligibility(current, registry)[0]:
        raise HarnessError("release_binding_revoked")
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Cache-Control": "no-store",
    })
