from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from app.api import deps
from app.api.deps import get_current_user, get_database_service, require_admin_user
from app.api.routes_report_runs import HarnessRoute
from app.report_harness.config import ReportHarnessSettings
from app.report_harness.errors import HarnessError
from app.report_harness.feedback import FeedbackStore, FeedbackTag, FeedbackVerdict, FeedbackWriteRequest
from app.report_harness.store import RunStore
from app.services.auth_service import AuthenticatedUser


def get_feedback_store(database=Depends(get_database_service)) -> FeedbackStore:
    """反馈只读写数据库，不依赖外发运行时；资源不足或模型不可用时仍可填写。"""
    config = ReportHarnessSettings.model_validate(
        getattr(deps.get_settings(), "report_harness", ReportHarnessSettings())
    )
    if not config.enabled:
        raise HarnessError("feature_unavailable", 503)
    store = RunStore(database.connection)
    store.check_schema()
    return FeedbackStore(store)


router = APIRouter(prefix="/api/v1/report-runs", tags=["report-feedback"], route_class=HarnessRoute)
admin_router = APIRouter(prefix="/api/v1/admin/report-feedback", tags=["admin"], route_class=HarnessRoute)


@router.get("/{run_id}/feedback")
def get_feedback(run_id: UUID, user: AuthenticatedUser = Depends(get_current_user),
                 store: FeedbackStore = Depends(get_feedback_store)):
    return store.get(user.id, str(run_id))


@router.put("/{run_id}/feedback")
def save_feedback(run_id: UUID, payload: FeedbackWriteRequest,
                  user: AuthenticatedUser = Depends(get_current_user),
                  store: FeedbackStore = Depends(get_feedback_store)):
    return store.save(user.id, str(run_id), payload)


@admin_router.get("")
def list_feedback(verdict: FeedbackVerdict | None = None, tag: FeedbackTag | None = None,
                  limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                  _admin: AuthenticatedUser = Depends(require_admin_user),
                  store: FeedbackStore = Depends(get_feedback_store)):
    return store.list_latest(verdict=verdict, tag=tag, limit=limit, offset=offset)


@admin_router.get("/export")
def export_feedback(verdict: FeedbackVerdict | None = None, tag: FeedbackTag | None = None,
                    _admin: AuthenticatedUser = Depends(require_admin_user),
                    store: FeedbackStore = Depends(get_feedback_store)):
    return Response(store.export_csv(verdict=verdict, tag=tag), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="report-feedback.csv"',
                             "Cache-Control": "no-store"})
