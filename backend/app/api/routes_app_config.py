from fastapi import APIRouter, Depends

from app.api.deps import get_current_user, get_settings
from app.core.settings import Settings
from app.schemas.workflow import (
    PublicAppConfigResponse,
    PublicReportModelResponse,
    PublicUploadLimitsResponse,
)
from app.services.auth_service import AuthenticatedUser

router = APIRouter(prefix="/api/v1/app-config", tags=["app-config"])


@router.get("", response_model=PublicAppConfigResponse)
def get_public_app_config(
    settings: Settings = Depends(get_settings),
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    return PublicAppConfigResponse(
        upload_limits=_build_public_upload_limits(settings),
        report_model=_build_public_report_model(settings, current_user=current_user),
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


def _build_public_report_model(
    settings: Settings,
    *,
    current_user: AuthenticatedUser,
) -> PublicReportModelResponse:
    _ = settings
    _ = current_user
    # 报告档位 gear 已下线：报告端点收敛为唯一 deepseek-v4-pro，按 per-user 能力配置解析。
    return PublicReportModelResponse(current_label=None, updated_at=None, options=[])
