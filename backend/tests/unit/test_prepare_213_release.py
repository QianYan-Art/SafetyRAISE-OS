import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[3] / "deployment" / "docker" / "prepare-213-release.py"
SPEC = importlib.util.spec_from_file_location("prepare_213_release", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def release():
    return {
        "services": {
            "backend": {
                "image": "sha256:" + "a" * 64,
                "read_only": True,
                "cap_drop": ["ALL"],
                "container_name": "docker_backend_1",
                "environment": {"DATABASE_DSN": "postgresql://user:secret@172.17.0.1:15432/safetyraise"},
                "volumes": [
                    f"/old/{name}:{target}:{module.MOUNT_MODES[target]}"
                    for target, name in module.BACKEND_MOUNTS.items()
                ],
            },
            "frontend": {
                "image": "sha256:" + "b" * 64,
                "container_name": "docker_frontend_1",
                "ports": ["0.0.0.0:80:80", "0.0.0.0:443:443"],
                "volumes": [
                    f"/old/{index}:{target}:{module.MOUNT_MODES[target]}"
                    for index, target in enumerate(module.FRONTEND_MOUNTS)
                ],
            },
        }
    }


def test_production_is_loopback_only_and_uses_local_database_network():
    config = module.prepare_release(
        release(),
        app_root=Path("/srv/apps/safetyraise"),
        data_root=Path("/srv/data/safetyraise"),
        local_data_root=Path("/srv/data/safetyraise-app"),
        release_dir=Path("/srv/apps/safetyraise/releases/release-1"),
        port=18080,
        backend_image="sha256:" + "c" * 64,
        frontend_image="sha256:" + "d" * 64,
    )
    backend = config["services"]["backend"]
    frontend = config["services"]["frontend"]
    assert frontend["ports"] == ["127.0.0.1:18080:80"]
    assert backend["image"] == "sha256:" + "c" * 64
    assert frontend["image"] == "sha256:" + "d" * 64
    assert backend["environment"]["DATABASE_DSN"] == "postgresql://user:secret@safetyraise-postgres:5432/safetyraise"
    assert backend["networks"] == ["default", "database"]
    assert config["networks"]["database"]["name"] == "safetyraise-data_default"
    assert "container_name" not in backend and "container_name" not in frontend
    assert "/srv/data/safetyraise/runtime:/app/backend/data:rw" in backend["volumes"]
    assert not any("/etc/letsencrypt" in mount for mount in frontend["volumes"])


def test_test_mode_isolates_writable_directories_and_database():
    config = module.prepare_release(
        release(),
        app_root=Path("/srv/apps/safetyraise"),
        data_root=Path("/srv/data/safetyraise"),
        local_data_root=Path("/srv/data/safetyraise-app"),
        release_dir=Path("/srv/apps/safetyraise/releases/release-1"),
        port=18081,
        backend_image="sha256:" + "c" * 64,
        frontend_image="sha256:" + "d" * 64,
        test_database="safetyraise_migration_test",
    )
    backend = config["services"]["backend"]
    assert backend["environment"]["DATABASE_DSN"].endswith("/safetyraise_migration_test")
    assert "/srv/data/safetyraise-app/test/runtime:/app/backend/data:rw" in backend["volumes"]
    assert "/srv/data/safetyraise-app/test/ledger:/var/lib/safetyraise/ledger:rw" in backend["volumes"]
    assert "/srv/data/safetyraise-app/test/tmp_uploads:/var/tmp/safetyraise-upload:rw" in backend["volumes"]


def test_unknown_mount_or_missing_sandbox_fails_closed():
    config = release()
    config["services"]["backend"]["volumes"].append("/unexpected:/root:rw")
    with pytest.raises(ValueError, match="Unexpected"):
        module.prepare_release(
            config,
            app_root=Path("/srv/apps/safetyraise"),
            data_root=Path("/srv/data/safetyraise"),
            local_data_root=Path("/srv/data/safetyraise-app"),
            release_dir=Path("/srv/apps/safetyraise/releases/release-1"),
            port=18080,
            backend_image="sha256:" + "c" * 64,
            frontend_image="sha256:" + "d" * 64,
        )


    config = release()
    config["services"]["backend"]["read_only"] = False
    with pytest.raises(ValueError, match="sandbox"):
        module.prepare_release(
            config,
            app_root=Path("/srv/apps/safetyraise"),
            data_root=Path("/srv/data/safetyraise"),
            local_data_root=Path("/srv/data/safetyraise-app"),
            release_dir=Path("/srv/apps/safetyraise/releases/release-1"),
            port=18080,
            backend_image="sha256:" + "c" * 64,
            frontend_image="sha256:" + "d" * 64,
        )


def test_isolated_database_name_rejects_non_ascii():
    with pytest.raises(ValueError, match="Invalid isolated database"):
        module.database_dsn(
            "postgresql://user:secret@localhost/safetyraise",
            name="\u6d4b\u8bd5",
        )
