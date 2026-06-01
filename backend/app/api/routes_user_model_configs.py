from fastapi import APIRouter, Depends

from app.api.deps import get_current_user, get_user_capability_config_service
from app.schemas.user_config import (
    CapabilityConfigStateResponse,
    UpdateCapabilityConfigsRequest,
)
from app.services.auth_service import AuthenticatedUser
from app.services.user_capability_config_service import UserCapabilityConfigService

router = APIRouter(prefix="/api/v1/user/model-configs", tags=["user-model-configs"])


@router.get("", response_model=CapabilityConfigStateResponse)
def get_user_capability_configs(
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: UserCapabilityConfigService = Depends(get_user_capability_config_service),
):
    return service.get_state(current_user)


@router.put("", response_model=CapabilityConfigStateResponse)
def update_user_capability_configs(
    request: UpdateCapabilityConfigsRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: UserCapabilityConfigService = Depends(get_user_capability_config_service),
):
    return service.update_state(user=current_user, request=request)
