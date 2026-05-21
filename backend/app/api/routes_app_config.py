from fastapi import APIRouter, Depends

from app.api.deps import get_settings
from app.core.settings import Settings
from app.schemas.workflow import (
    PublicAppConfigResponse,
    PublicReportModelOptionResponse,
    PublicReportModelResponse,
    PublicUploadLimitsResponse,
    UpdateReportModelSelectionRequest,
)
from app.services.report_model_selection_service import ReportModelSelectionService

router = APIRouter(prefix="/api/v1/app-config", tags=["app-config"])


@router.get("", response_model=PublicAppConfigResponse)
def get_public_app_config(
    settings: Settings = Depends(get_settings),
):
    return PublicAppConfigResponse(
        upload_limits=_build_public_upload_limits(settings),
        report_model=_build_public_report_model(settings),
    )


@router.put("/report-model", response_model=PublicReportModelResponse)
def update_report_model(
    request: UpdateReportModelSelectionRequest,
    settings: Settings = Depends(get_settings),
):
    service = ReportModelSelectionService(settings)
    payload = service.set_selected_label(request.label)
    return PublicReportModelResponse(
        current_label=payload["current_label"],
        updated_at=payload.get("updated_at"),
        options=[
            PublicReportModelOptionResponse(**option)
            for option in payload.get("options", [])
        ],
    )


def _build_public_upload_limits(settings: Settings) -> PublicUploadLimitsResponse:
    return PublicUploadLimitsResponse(
        max_total_bytes=settings.input_generation.upload.max_total_bytes,
        max_image_bytes=settings.input_generation.upload.max_image_bytes,
        max_video_bytes=settings.input_generation.upload.max_video_bytes,
        max_model_images=settings.input_generation.upload.max_model_images,
        max_images_per_group=settings.input_generation.upload.max_images_per_group,
        max_videos_per_group=settings.input_generation.upload.max_videos_per_group,
        max_total_images=settings.input_generation.upload.max_total_images,
        max_total_videos=settings.input_generation.upload.max_total_videos,
    )


def _build_public_report_model(settings: Settings) -> PublicReportModelResponse:
    payload = ReportModelSelectionService(settings).get_public_state()
    return PublicReportModelResponse(
        current_label=payload["current_label"],
        updated_at=payload.get("updated_at"),
        options=[
            PublicReportModelOptionResponse(**option)
            for option in payload.get("options", [])
        ],
    )
