import ipaddress

from fastapi import APIRouter, Depends, Request

from app.api.deps import get_auth_service, get_current_user
from app.schemas.auth import (
    AuthTokenResponse,
    ChangePasswordRequest,
    LoginRequest,
    RegisterRequest,
    UserSummaryResponse,
)
from app.services.auth_service import AuthService, AuthenticatedUser

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


_TRUSTED_PROXY_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_trusted_proxy_ip(raw_ip: str) -> bool:
    try:
        ip_obj = ipaddress.ip_address(raw_ip.strip())
    except ValueError:
        return False
    return any(ip_obj in network for network in _TRUSTED_PROXY_NETWORKS)


def _parse_valid_ip(raw_ip: str | None) -> str | None:
    candidate = str(raw_ip or "").strip()
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def _extract_client_ip(request: Request) -> str:
    direct_ip = request.client.host if (request.client and request.client.host) else ""
    if direct_ip and _is_trusted_proxy_ip(direct_ip):
        # 仅在来自回环或容器私网反代时采信宿主 Nginx 覆写的真实客户端 IP。
        real_ip = _parse_valid_ip(request.headers.get("x-real-ip"))
        if real_ip:
            return real_ip
        forwarded_for = request.headers.get("x-forwarded-for") or ""
        if forwarded_for:
            first_hop = _parse_valid_ip(forwarded_for.split(",", 1)[0])
            if first_hop:
                return first_hop
    return _parse_valid_ip(direct_ip) or direct_ip or "unknown"


@router.post("/register", response_model=AuthTokenResponse)
def register(
    http_request: Request,
    request: RegisterRequest,
    service: AuthService = Depends(get_auth_service),
):
    return service.register(request, client_ip=_extract_client_ip(http_request))


@router.post("/login", response_model=AuthTokenResponse)
def login(
    http_request: Request,
    request: LoginRequest,
    service: AuthService = Depends(get_auth_service),
):
    return service.login(request, client_ip=_extract_client_ip(http_request))


@router.post("/logout")
def logout(
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
):
    service.logout(current_user.id)
    return {"status": "success"}


@router.post("/change-password", response_model=AuthTokenResponse)
def change_password(
    request: ChangePasswordRequest,
    current_user: AuthenticatedUser = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
):
    return service.change_password(current_user.id, request)


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
