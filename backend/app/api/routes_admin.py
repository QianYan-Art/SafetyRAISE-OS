from fastapi import APIRouter, Depends

from app.api.deps import get_admin_service, require_admin_user
from app.schemas.admin import (
    AdminCleanupSpacesResponse,
    AdminCreateUserRequest,
    AdminSpaceRecord,
    AdminUpdateSpaceRequest,
    AdminUpdateUserRequest,
    AdminUserResponse,
)
from app.services.admin_service import AdminService
from app.services.auth_service import AuthenticatedUser

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@router.get("/users", response_model=list[AdminUserResponse])
def list_users(
    _current_user: AuthenticatedUser = Depends(require_admin_user),
    service: AdminService = Depends(get_admin_service),
):
    return service.list_users()


@router.post("/users", response_model=AdminUserResponse)
def create_user(
    request: AdminCreateUserRequest,
    _current_user: AuthenticatedUser = Depends(require_admin_user),
    service: AdminService = Depends(get_admin_service),
):
    return service.create_user(request)


@router.put("/users/{user_id}", response_model=AdminUserResponse)
def update_user(
    user_id: str,
    request: AdminUpdateUserRequest,
    _current_user: AuthenticatedUser = Depends(require_admin_user),
    service: AdminService = Depends(get_admin_service),
):
    return service.update_user(user_id, request)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: str,
    current_user: AuthenticatedUser = Depends(require_admin_user),
    service: AdminService = Depends(get_admin_service),
):
    service.delete_user(user_id, current_user)
    return {"status": "success"}


@router.get("/spaces", response_model=list[AdminSpaceRecord])
def list_spaces(
    _current_user: AuthenticatedUser = Depends(require_admin_user),
    service: AdminService = Depends(get_admin_service),
):
    return service.list_spaces()


@router.patch("/spaces/{session_id}", response_model=AdminSpaceRecord)
def update_space(
    session_id: str,
    request: AdminUpdateSpaceRequest,
    _current_user: AuthenticatedUser = Depends(require_admin_user),
    service: AdminService = Depends(get_admin_service),
):
    return service.update_space(session_id, request)


@router.delete("/spaces/{session_id}")
def delete_space(
    session_id: str,
    _current_user: AuthenticatedUser = Depends(require_admin_user),
    service: AdminService = Depends(get_admin_service),
):
    service.delete_space(session_id)
    return {"status": "success"}


@router.post("/spaces/cleanup-orphans", response_model=AdminCleanupSpacesResponse)
def cleanup_orphan_spaces(
    _current_user: AuthenticatedUser = Depends(require_admin_user),
    service: AdminService = Depends(get_admin_service),
):
    return AdminCleanupSpacesResponse(deleted_count=service.cleanup_orphan_spaces())
