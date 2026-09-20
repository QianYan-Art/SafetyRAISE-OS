from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = ROOT / "deployment/docker/docker-compose.server.yml"
HARNESS_COMPOSE = ROOT / "deployment/docker/docker-compose.harness.yml"
BACKEND_RUNTIME_DOCKERFILE = ROOT / "deployment/docker/backend.runtime.Dockerfile"
FRONTEND_RUNTIME_DOCKERFILE = ROOT / "deployment/docker/frontend.runtime.Dockerfile"
DEFAULT_NGINX_CONFIG = ROOT / "deployment/docker/nginx.frontend.conf"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_harness_override_is_explicit_and_fail_closed():
    base = _load(BASE_COMPOSE)
    override = _load(HARNESS_COMPOSE)

    assert "docker-compose.harness.yml" not in BASE_COMPOSE.read_text(encoding="utf-8")
    assert base["services"]["backend"]["environment"]["WORKFLOW_CONFIG_PATH"] == (
        "/app/backend/config/workflow.server.yaml"
    )
    assert base["services"]["backend"]["build"]["args"]["INSTALL_VIDEO_DEPS"] == (
        "${INSTALL_VIDEO_DEPS:-false}"
    )
    assert "user" not in base["services"]["backend"]
    assert "/run/safetyraise/harness" not in BASE_COMPOSE.read_text(encoding="utf-8")
    assert "/var/lib/safetyraise/ledger" not in BASE_COMPOSE.read_text(encoding="utf-8")

    backend = override["services"]["backend"]
    backend_build = backend["build"]
    backend_dockerfile = BACKEND_RUNTIME_DOCKERFILE.read_text(encoding="utf-8")
    assert backend_build["context"] == "../.."
    assert backend_build["dockerfile"] == "deployment/docker/backend.runtime.Dockerfile"
    assert backend["build"]["args"]["INSTALL_VIDEO_DEPS"] == "true"
    assert backend_build["args"]["RUNTIME_BASE_IMAGE"] == (
        "${BACKEND_RUNTIME_BASE_IMAGE:?set the verified local backend image ID}"
    )
    assert backend["image"] == "safetyraise-backend:harness-runtime"
    assert "ARG RUNTIME_BASE_IMAGE" in backend_dockerfile
    assert "FROM ${RUNTIME_BASE_IMAGE}" in backend_dockerfile
    assert backend["user"] == "${HARNESS_UID:-10001}:${HARNESS_GID:-10001}"
    assert backend["read_only"] is True
    assert backend["cap_drop"] == ["ALL"]
    assert backend["security_opt"] == ["no-new-privileges:true"]
    assert backend["tmpfs"] == ["/tmp:rw,nosuid,nodev,noexec,size=128m"]
    assert backend["environment"]["WORKFLOW_CONFIG_PATH"] == (
        "/run/safetyraise/harness/workflow.server.yaml"
    )
    assert backend["environment"]["TMPDIR"] == "/var/lib/safetyraise/harness-tmp"
    assert "INSTALL_VIDEO_DEPS" not in backend["environment"]
    assert backend["command"][-2:] == [
        "--config",
        "/run/safetyraise/harness/workflow.server.yaml",
    ]
    assert "REPORT_HARNESS_ENABLED" not in backend["environment"]

    volumes = backend["volumes"]
    assert any(
        item.startswith("${HARNESS_WORKFLOW_CONFIG_HOST_PATH:?")
        and item.endswith(":/run/safetyraise/harness/workflow.server.yaml:ro")
        for item in volumes
    )
    assert any(
        item.startswith("${HARNESS_MANIFEST_HOST_PATH:?")
        and item.endswith(":/run/safetyraise/harness/runtime-manifest.json:ro")
        for item in volumes
    )
    assert any(
        item.startswith("${HARNESS_RELEASE_DIR_HOST_PATH:?")
        and item.endswith(":/app/backend/config/report_harness:ro")
        for item in volumes
    )
    assert any(
        item.startswith("${HARNESS_LOCAL_LEDGER_HOST_DIR:?")
        and item.endswith(":/var/lib/safetyraise/ledger:rw")
        for item in volumes
    )
    assert any(
        item.startswith("${HARNESS_LOCAL_TMP_HOST_DIR:?")
        and item.endswith(":/var/lib/safetyraise/harness-tmp:rw")
        for item in volumes
    )
    assert "/var/lib/safetyraise/harness-tmp" != "/var/lib/safetyraise/ledger"

    assert not any(
        ":/app/backend/data" in item and "HARNESS_LOCAL_LEDGER_HOST_DIR" in item
        for item in volumes
    )


def test_frontend_runtime_override_is_offline_and_keeps_upload_limit():
    base = _load(BASE_COMPOSE)
    override = _load(HARNESS_COMPOSE)
    frontend = override["services"]["frontend"]
    build = frontend["build"]
    dockerfile = FRONTEND_RUNTIME_DOCKERFILE.read_text(encoding="utf-8")

    assert base["services"]["frontend"]["build"]["dockerfile"] == (
        "deployment/docker/frontend.Dockerfile"
    )
    assert build["context"] == (
        "${FRONTEND_RUNTIME_CONTEXT_HOST_PATH:?set the prepared frontend runtime context directory}"
    )
    assert build["dockerfile"] == "Dockerfile"
    assert "additional_contexts" not in build
    assert build["args"]["RUNTIME_BASE_IMAGE"] == (
        "${FRONTEND_RUNTIME_BASE_IMAGE:?set the verified local nginx image ID}"
    )
    assert "ARG RUNTIME_BASE_IMAGE" in dockerfile
    assert "FROM ${RUNTIME_BASE_IMAGE}" in dockerfile
    assert "COPY dist /usr/share/nginx/html" in dockerfile
    assert "COPY nginx.frontend.conf /etc/nginx/conf.d/default.conf" in dockerfile
    assert "frontend-dist" not in dockerfile
    dockerfile_lower = dockerfile.lower()
    assert "from node" not in dockerfile_lower
    assert "run npm" not in dockerfile_lower
    assert "npm ci" not in dockerfile_lower

    frontend_volumes = base["services"]["frontend"]["volumes"]
    assert frontend_volumes
    assert all(item.endswith(":ro") for item in frontend_volumes)
    assert all("HARNESS_LOCAL_TMP_HOST_DIR" not in item for item in frontend_volumes)
    assert "client_max_body_size 1200m;" in DEFAULT_NGINX_CONFIG.read_text(encoding="utf-8")
