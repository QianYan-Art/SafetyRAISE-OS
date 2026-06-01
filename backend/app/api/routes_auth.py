from fastapi import APIRouter, Depends

from app.api.deps import get_auth_service, get_current_user
from app.schemas.auth import AuthTokenResponse, LoginRequest, RegisterRequest, UserSummaryResponse
from app.services.auth_service import AuthService, AuthenticatedUser

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/register", response_model=AuthTokenResponse)
def register(
    request: RegisterRequest,
    service: AuthService = Depends(get_auth_service),
):
    return service.register(request)


@router.post("/login", response_model=AuthTokenResponse)
def login(
    request: LoginRequest,
    service: AuthService = Depends(get_auth_service),
):
    return service.login(request)


@router.get("/me", response_model=UserSummaryResponse)
def get_me(
    current_user: AuthenticatedUser = Depends(get_current_user),
):
    return UserSummaryResponse(
        id=current_user.id,
        username=current_user.username,
        display_name=current_user.display_name,
        role=current_user.role,
        is_active=current_user.is_active,
        created_at=current_user.created_at,
        updated_at=current_user.updated_at,
    )
