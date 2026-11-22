"""针对安全审计发现的核心安全回归测试集。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.api import deps, routes_chat_sessions
from app.api.error_handling import register_exception_handlers
from app.api.routes_input import (
    _detect_magic_media_type,
    _detect_media_type,
    _parse_upload_manifest,
)
from app.core.auth_throttle import get_auth_rate_limiter, reset_auth_rate_limiter
from app.core.exceptions import (
    AuthenticationError,
    InputValidationError,
    PermissionDeniedError,
    ProviderError,
    RateLimitExceededError,
    WorkflowError,
)
from app.core.json_parser import extract_json_from_text
from app.core.media_access import decode_media_token, issue_media_token
from app.core.model_output import sanitize_model_text
from app.core.outbound_url import request_json_bounded, validate_user_outbound_url
from app.core.security import (
    create_access_token,
    decode_access_token,
    verify_password_constant_time,
)
from app.core.settings import AuthSettings, Settings
from app.schemas.admin import AdminUpdateUserRequest
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.admin_service import AdminService
from app.services.auth_service import AuthService, AuthenticatedUser
from app.services.chat_session_service import ChatSessionService
from app.services.report_export_service import ReportExportService, ResolvedPdfCoverConfig
from app.services.report_service import ReportService


def _load_dev_settings() -> Settings:
    config_path = Path(__file__).resolve().parents[2] / "config" / "workflow.yaml"
    return Settings.model_validate(yaml.safe_load(config_path.read_text("utf-8")))


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000/v1",
        "https://169.254.169.254/latest/meta-data",
        "https://10.0.0.1/v1",
        "https://192.168.1.10/v1",
        "https://2130706433/v1",
        "https://localhost/v1",
        "https://user:pass@api.openai.com/v1",
        "https://api.openai.com/v1?foo=bar",
        "http://api.openai.com/v1",
    ],
)
def test_outbound_url_validator_blocks_ssrf_and_insecure_http(bad_url):
    with pytest.raises(InputValidationError):
        validate_user_outbound_url(bad_url, allow_http=False, host_allowlist=(), resolve_dns=False)


def test_outbound_url_validator_allows_https_and_explicit_allowlist():
    assert validate_user_outbound_url(
        "https://api.openai.com/v1", allow_http=False, host_allowlist=(), resolve_dns=False
    ) == "https://api.openai.com/v1"
    assert validate_user_outbound_url(
        "http://127.0.0.1:1234/v1",
        allow_http=False,
        host_allowlist=("127.0.0.1:1234",),
        resolve_dns=False,
    ) == "http://127.0.0.1:1234/v1"


def test_embedding_override_never_leaks_system_api_key_env_to_custom_url(monkeypatch):
    import socket

    # 模拟解析到公网 IP，排除本机代理 Fake-IP 对测试的干扰。
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    settings = _load_dev_settings()
    settings.models.retrieval_embedding.api_key_env = "OPENROUTER_API_KEY"
    overridden = ReportService._apply_embedding_override(
        settings,
        {"base_url": "https://attacker.example/v1"},
    )
    assert overridden.models.retrieval_embedding.base_url == "https://attacker.example/v1"
    assert overridden.models.retrieval_embedding.api_key == ""
    assert overridden.models.retrieval_embedding.api_key_env == ""


def test_outbound_url_validator_blocks_dns_rebinding_to_private_ip(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(InputValidationError):
        validate_user_outbound_url("https://rebind.example/v1", allow_http=False, host_allowlist=(), resolve_dns=True)


def test_bounded_json_request_rejects_oversized_chunked_response():
    class ChunkedStream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(12):
                yield b"a" * (128 * 1024)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkedStream())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as exc_info:
            request_json_bounded(client, "POST", "https://example.invalid/v1", max_bytes=1024 * 1024)
    assert exc_info.value.code == "PROVIDER_RESPONSE_TOO_LARGE"


def test_password_schema_rejects_oversized_passwords():
    huge = "A" * 65536
    with pytest.raises(ValueError):
        LoginRequest(username="alice", password=huge)
    with pytest.raises(ValueError):
        RegisterRequest(username="alice", password=huge)


def test_verify_password_constant_time_handles_missing_hash():
    assert verify_password_constant_time("wrong-password", None) is False
    assert verify_password_constant_time("wrong-password", "") is False


def test_auth_rate_limiter_enforces_limit_with_retry_after():
    reset_auth_rate_limiter()
    limiter = get_auth_rate_limiter()
    for _ in range(3):
        limiter.check_and_record(bucket_key="login:test", limit=3, window_seconds=60, now=100.0)
    with pytest.raises(RateLimitExceededError) as exc_info:
        limiter.check_and_record(bucket_key="login:test", limit=3, window_seconds=60, now=110.0)
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after >= 1
    reset_auth_rate_limiter()


_TEST_JWT_SECRET = "".join(chr(ord("a") + i) for i in range(26)) + "0123456789"


def test_access_token_requires_audience_and_media_token_is_session_scoped():
    auth = AuthSettings(jwt_secret=_TEST_JWT_SECRET)
    token_without_aud = jwt.encode(
        {"sub": "u1", "iat": 1700000000, "exp": 9999999999},
        auth.jwt_secret,
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationError):
        decode_access_token(token_without_aud, auth)

    media_token = issue_media_token(auth, "u1", session_id="sess-a")
    assert decode_media_token(auth, media_token, expected_session_id="sess-a") == "u1"
    with pytest.raises(AuthenticationError):
        decode_media_token(auth, media_token, expected_session_id="sess-b")
    with pytest.raises(AuthenticationError):
        decode_access_token(media_token, auth)


def test_register_blocks_bootstrap_admin_username_squatting():
    class UnusedDB:
        @contextmanager
        def connection(self):
            class Cur:
                def execute(self, *_args, **_kwargs):
                    return None

                def fetchone(self):
                    return None

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            class Conn:
                def cursor(self):
                    return Cur()

                def commit(self):
                    return None

            yield Conn()

    settings = SimpleNamespace(
        auth=AuthSettings(
            jwt_secret=_TEST_JWT_SECRET,
            bootstrap_admin_username="safetyraise",
            bootstrap_admin_password="",
            login_rate_limit_enabled=False,
        )
    )
    service = AuthService(settings, UnusedDB())
    with pytest.raises(InputValidationError):
        service.register(RegisterRequest(username="safetyraise", password="PrivatePassword123"))


def test_admin_update_user_protects_bootstrap_and_last_admin():
    class FakeDB:
        def __init__(self, target_row, remaining_admins):
            self.target_row = target_row
            self.remaining_admins = remaining_admins

        @contextmanager
        def connection(self):
            db = self

            class Cur:
                def __init__(self):
                    self.last_sql = ""

                def execute(self, sql, params=None):
                    self.last_sql = " ".join(sql.lower().split())

                def fetchone(self):
                    if "select count(*)" in self.last_sql:
                        return {"cnt": db.remaining_admins}
                    return db.target_row

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            class Conn:
                def cursor(self):
                    return Cur()

                def commit(self):
                    return None

            yield Conn()

    settings = SimpleNamespace(auth=AuthSettings(bootstrap_admin_username="safetyraise"))
    actor = AuthenticatedUser(
        id="admin-1", username="admin1", display_name=None, role="admin",
        is_active=True, created_at="", updated_at="",
    )

    # 禁止降权 bootstrap 管理员
    svc_boot = AdminService(
        settings,
        FakeDB({"id": "boot-1", "username": "safetyraise", "role": "admin", "is_active": True}, remaining_admins=2),
    )
    with pytest.raises(PermissionDeniedError):
        svc_boot.update_user("boot-1", AdminUpdateUserRequest(role="user"), current_user=actor)

    # 禁止自停用
    svc_self = AdminService(
        settings,
        FakeDB({"id": "admin-1", "username": "admin1", "role": "admin", "is_active": True}, remaining_admins=2),
    )
    with pytest.raises(PermissionDeniedError):
        svc_self.update_user("admin-1", AdminUpdateUserRequest(is_active=False), current_user=actor)

    # 禁止降权最后一名活跃管理员
    svc_last = AdminService(
        settings,
        FakeDB({"id": "admin-2", "username": "admin2", "role": "admin", "is_active": True}, remaining_admins=0),
    )
    with pytest.raises(PermissionDeniedError):
        svc_last.update_user("admin-2", AdminUpdateUserRequest(role="user"), current_user=actor)


def test_chat_session_service_is_fail_closed_without_user():
    settings = _load_dev_settings()
    with pytest.raises(AuthenticationError):
        ChatSessionService(settings=settings)


def test_upload_validation_blocks_spoofed_active_content_and_bad_manifest_int():
    # 伪造 MIME 的 .html / .svg 必须被拒绝
    with pytest.raises(WorkflowError):
        _detect_media_type("image/png", "poc.html")
    with pytest.raises(WorkflowError):
        _detect_media_type("image/png", "poc.svg")

    # 魔数检测：真实 PNG 与 MP4 通过，HTML 内容拒绝
    assert _detect_magic_media_type(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16) == "image"
    assert _detect_magic_media_type(b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00") == "video"
    assert _detect_magic_media_type(b"<html><script>alert(1)</script></html>") is None

    # 非法整数字段应返回 400 InputValidationError 而非未捕获 ValueError
    manifest = (
        '{"groups": [{"category_id": "scene", "category_label": "现场", "sequence": "bad"}],'
        ' "items": [{"category_id": "scene", "media_type": "image", "original_name": "a.png"}]}'
    )
    with pytest.raises(InputValidationError):
        _parse_upload_manifest(manifest, expected_count=1)


def test_media_asset_route_forces_attachment_and_sandbox_for_active_content(tmp_path, monkeypatch):
    html_file = tmp_path / "poc.html"
    html_file.write_text("<script>alert(1)</script>", encoding="utf-8")

    class StubSessions:
        def resolve_linked_artifact_asset(self, session_id, category, asset_id):
            return SimpleNamespace(path=str(html_file), mime_type="text/html", file_name="poc.html")

    app = FastAPI()
    app.include_router(routes_chat_sessions.router)
    app.dependency_overrides[routes_chat_sessions.get_media_chat_session_service] = lambda: StubSessions()
    with TestClient(app) as client:
        response = client.get("/api/v1/chat-sessions/s1/linked-artifacts/images_and_keyframes/assets/a1")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/octet-stream")
    assert "attachment" in response.headers["content-disposition"].lower()
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


@pytest.mark.parametrize("payload", ["{[]}", "prefix tail {,'"])
def test_json_parser_rejects_malformed_fragments(payload):
    with pytest.raises(InputValidationError):
        extract_json_from_text(payload)


@pytest.mark.parametrize("payload", ["{[]}", "{{}}", "{1,{}}"])
def test_json_parser_never_raises_unexpected_typeerror(payload):
    try:
        result = extract_json_from_text(payload)
        assert isinstance(result, dict)
    except InputValidationError:
        pass


def test_sanitize_model_text_strips_unclosed_reasoning_tags_and_fences():
    assert sanitize_model_text("正文结果<think>未闭合的内部思考过程") == "正文结果"
    assert sanitize_model_text("正文结果\n```thinking\n未闭合思考围栏") == "正文结果"


def test_ready_endpoint_blocks_anonymous_active_probe_and_sets_security_headers():
    called_flags: list[bool] = []

    class RecordingReadiness:
        def check(self, *, active_external_probes: bool = False):
            called_flags.append(active_external_probes)
            return {"status": "ready", "ready": True, "checks": {}}

    main.app.dependency_overrides[deps.get_readiness_service] = lambda: RecordingReadiness()
    main.app.dependency_overrides[deps.get_optional_current_user] = lambda: None
    try:
        with TestClient(main.app) as client:
            health_resp = client.get("/api/v1/health")
            assert health_resp.status_code == 200
            assert health_resp.headers["x-content-type-options"] == "nosniff"
            assert health_resp.headers["x-frame-options"] == "DENY"
            assert health_resp.headers["referrer-policy"] == "no-referrer"

            ready_resp = client.get("/api/v1/ready")
            assert ready_resp.status_code == 200
            assert called_flags == [False]

            active_resp = client.get("/api/v1/ready?probe=active")
            assert active_resp.status_code == 403

            oversized_auth = client.post(
                "/api/v1/auth/login",
                content=b'{"username":"a","password":"' + b"x" * (65 * 1024) + b'"}',
                headers={"Content-Type": "application/json"},
            )
            assert oversized_auth.status_code == 413
    finally:
        main.app.dependency_overrides.pop(deps.get_readiness_service, None)
        main.app.dependency_overrides.pop(deps.get_optional_current_user, None)


def test_pdf_cover_story_escapes_xml_markup():
    exporter = ReportExportService.__new__(ReportExportService)
    exporter._ensure_pdf_font_registered()
    styles = exporter._build_pdf_styles()
    cover = ResolvedPdfCoverConfig(
        title="事故报告 <font color=red>注入</font> & 测试",
        subtitle="<b>副标题</b> <unclosed",
        compiled_by="<a href='http://evil'>编制人</a>",
        date_text="2026-11-22 <br>",
        report_number="trace-123",
    )
    flowables = exporter._build_pdf_cover_story(styles, cover)
    assert len(flowables) >= 6


def test_extract_client_ip_trusts_proxy_headers_only_from_internal_proxy():
    from app.api.routes_auth import _extract_client_ip

    proxied_req = SimpleNamespace(
        client=SimpleNamespace(host="172.18.0.3"),
        headers={"x-real-ip": "203.0.113.42", "x-forwarded-for": "198.51.100.1, 172.18.0.3"},
    )
    assert _extract_client_ip(proxied_req) == "203.0.113.42"

    direct_public_req = SimpleNamespace(
        client=SimpleNamespace(host="198.51.100.99"),
        headers={"x-real-ip": "1.2.3.4", "x-forwarded-for": "5.6.7.8"},
    )
    assert _extract_client_ip(direct_public_req) == "198.51.100.99"


def test_admin_default_url_matching_normalizes_chat_completions_suffix():
    from app.services.user_capability_config_service import UserCapabilityConfigService

    settings = _load_dev_settings()
    svc = UserCapabilityConfigService(settings=settings, database_service=object())
    assert svc._matches_system_default_url("report", "https://openrouter.ai/api/v1") is True
    assert svc._matches_system_default_url("vision", "https://openrouter.ai/api/v1") is True
    assert svc._matches_system_default_url("report", "https://attacker.example/v1") is False
