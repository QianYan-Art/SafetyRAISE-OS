from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
BASE_COMPOSE = ROOT / "deployment/docker/docker-compose.server.yml"
HARNESS_COMPOSE = ROOT / "deployment/docker/docker-compose.harness.yml"


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
    assert backend["build"]["args"]["INSTALL_VIDEO_DEPS"] == "true"
    assert backend["user"] == "${HARNESS_UID:-10001}:${HARNESS_GID:-10001}"
    assert backend["read_only"] is True
    assert backend["cap_drop"] == ["ALL"]
    assert backend["security_opt"] == ["no-new-privileges:true"]
    assert backend["tmpfs"] == ["/tmp:rw,nosuid,nodev,noexec,size=128m"]
    assert backend["environment"]["WORKFLOW_CONFIG_PATH"] == (
        "/run/safetyraise/harness/workflow.server.yaml"
    )
    assert backend["environment"]["TMPDIR"] == "/tmp"
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

    assert not any(
        ":/app/backend/data" in item and "HARNESS_LOCAL_LEDGER_HOST_DIR" in item
        for item in volumes
    )
