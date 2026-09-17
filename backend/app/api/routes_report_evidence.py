from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.api.routes_report_runs import HarnessRoute, get_report_run_service
from app.report_harness.evidence import EvidenceWriteRequest
from app.report_harness.evidence_store import EvidenceStore
from app.services.auth_service import AuthenticatedUser
from app.services.report_run_service import ReportRunService

router = APIRouter(prefix="/api/v1/chat-sessions", tags=["report-evidence"], route_class=HarnessRoute)


@router.get("/{session_id}/report-evidence")
def get_evidence(session_id: str, user: AuthenticatedUser = Depends(get_current_user),
                 service: ReportRunService = Depends(get_report_run_service)):
    return EvidenceStore(service.store).get(user.id, session_id)


@router.put("/{session_id}/report-evidence")
def save_evidence(session_id: str, payload: EvidenceWriteRequest,
                  user: AuthenticatedUser = Depends(get_current_user),
                  service: ReportRunService = Depends(get_report_run_service)):
    return EvidenceStore(service.store).save(user.id, session_id, payload)
