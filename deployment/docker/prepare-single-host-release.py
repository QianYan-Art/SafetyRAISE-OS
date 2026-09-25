"""Adapt an inspected split-host release (app host + data host) to a single-host Docker/Nginx layout."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


BACKEND_MOUNTS = {
    "/app/backend/data": "runtime",
    "/opt/ts-analysis/models": "models",
    "/opt/ts-analysis/kbase": "kbase",
    "/var/tmp/safetyraise-upload": "uploads",
    "/var/lib/safetyraise/ledger": "ledger",
    "/app/backend/config/workflow.server.yaml": "workflow",
    "/run/safetyraise/harness/runtime-manifest.json": "manifest",
}
FRONTEND_MOUNTS = {
    "/etc/nginx/conf.d": "nginx",
    "/etc/letsencrypt": None,
    "/var/www/certbot": None,
}
MOUNT_MODES = {
    "/app/backend/data": "rw",
    "/opt/ts-analysis/models": "ro",
    "/opt/ts-analysis/kbase": "ro",
    "/var/tmp/safetyraise-upload": "rw",
    "/var/lib/safetyraise/ledger": "rw",
    "/app/backend/config/workflow.server.yaml": "ro",
    "/run/safetyraise/harness/runtime-manifest.json": "ro",
    "/etc/nginx/conf.d": "ro",
    "/etc/letsencrypt": "ro",
    "/var/www/certbot": "ro",
}


def database_dsn(original: str, *, name: str | None = None) -> str:
    parts = urlsplit(original)
    if parts.scheme not in {"postgresql", "postgres"} or not parts.username or not parts.path:
        raise ValueError("Unsupported production DATABASE_DSN")
    if name is not None and not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError("Invalid isolated database name")
    userinfo = parts.netloc.rsplit("@", 1)[0]
    return urlunsplit(
        (parts.scheme, f"{userinfo}@safetyraise-postgres:5432", f"/{name}" if name else parts.path, parts.query, parts.fragment)
    )


def rewrite_mounts(
    mounts: list[str], destinations: dict[str, str | None], sources: dict[str, Path]
) -> list[str]:
    result = []
    seen = set()
    for mount in mounts:
        fields = mount.split(":")
        if len(fields) != 3 or fields[1] not in destinations or fields[1] in seen:
            raise ValueError(f"Unexpected or duplicate mount destination: {fields[1] if len(fields) > 1 else mount}")
        destination = fields[1]
        if fields[2] != MOUNT_MODES[destination]:
            raise ValueError(f"Unexpected mount mode for {destination}")
        seen.add(destination)
        key = destinations[destination]
        if key is not None:
            result.append(f"{sources[key].as_posix()}:{destination}:{fields[2]}")
    if seen != set(destinations):
        raise ValueError(f"Missing mount destinations: {sorted(set(destinations) - seen)}")
    return result


def prepare_release(
    source: dict,
    *,
    app_root: Path,
    data_root: Path,
    local_data_root: Path,
    release_dir: Path,
    port: int,
    backend_image: str,
    frontend_image: str,
    test_database: str | None = None,
) -> dict:
    if not 1024 <= port <= 65535:
        raise ValueError("Frontend port must be an unprivileged local port")
    if set(source.get("services", {})) != {"backend", "frontend"}:
        raise ValueError("Release must contain exactly backend and frontend")

    backend = source["services"]["backend"]
    frontend = source["services"]["frontend"]
    if not backend.get("read_only") or not backend.get("cap_drop"):
        raise ValueError("Source backend is missing the verified sandbox settings")
    if not backend.get("image", "").startswith("sha256:") or not frontend.get("image", "").startswith("sha256:"):
        raise ValueError("Source images must be pinned by digest")
    for image in (backend_image, frontend_image):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("Target image must be pinned by a verified local ID")

    isolated = test_database is not None
    sources = {
        "runtime": local_data_root / "test" / "runtime" if isolated else data_root / "runtime",
        "models": local_data_root / "models",
        "kbase": data_root / "kbase",
        "uploads": local_data_root / "test" / "tmp_uploads" if isolated else local_data_root / "tmp_uploads",
        "ledger": local_data_root / "test" / "ledger" if isolated else local_data_root / "ledger",
        "workflow": release_dir / "workflow.server.yaml",
        "manifest": release_dir / "runtime-manifest.json",
        "nginx": app_root / "nginx-upstream",
    }
    backend["volumes"] = rewrite_mounts(backend["volumes"], BACKEND_MOUNTS, sources)
    frontend["volumes"] = rewrite_mounts(frontend["volumes"], FRONTEND_MOUNTS, sources)
    backend["environment"]["DATABASE_DSN"] = database_dsn(
        backend["environment"]["DATABASE_DSN"], name=test_database
    )
    backend["environment"]["BACKEND_DATA_HOST_PATH"] = sources["runtime"].as_posix()
    backend["environment"]["KBASE_HOST_PATH"] = sources["kbase"].as_posix()
    backend["environment"]["MODELS_HOST_PATH"] = sources["models"].as_posix()
    backend["environment"]["FRONTEND_NGINX_HOST_PATH"] = sources["nginx"].as_posix()
    backend["environment"]["TMPDIR"] = "/var/tmp/safetyraise-upload"
    backend["networks"] = ["default", "database"]
    frontend["networks"] = ["default"]
    frontend["ports"] = [f"127.0.0.1:{port}:80"]
    backend["image"] = backend_image
    frontend["image"] = frontend_image
    for service in (backend, frontend):
        service.pop("container_name", None)
    source.pop("version", None)
    source["networks"] = {
        "default": {"driver": "bridge"},
        "database": {"external": True, "name": "safetyraise-data_default"},
    }
    return source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--app-root", type=Path, default=Path("/srv/apps/safetyraise"))
    parser.add_argument("--data-root", type=Path, default=Path("/srv/data/safetyraise"))
    parser.add_argument("--local-data-root", type=Path, default=Path("/srv/data/safetyraise-app"))
    parser.add_argument("--frontend-port", type=int, required=True)
    parser.add_argument("--backend-image", required=True)
    parser.add_argument("--frontend-image", required=True)
    parser.add_argument("--test-database", help="Use isolated database, runtime, uploads and ledger")
    args = parser.parse_args()

    with args.source.open(encoding="utf-8") as handle:
        source = json.load(handle)
    result = prepare_release(
        source,
        app_root=args.app_root,
        data_root=args.data_root,
        local_data_root=args.local_data_root,
        release_dir=args.release_dir,
        port=args.frontend_port,
        backend_image=args.backend_image,
        frontend_image=args.frontend_image,
        test_database=args.test_database,
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(args.output, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"Prepared {args.output} (contains secrets; mode 0600)")


if __name__ == "__main__":
    main()
