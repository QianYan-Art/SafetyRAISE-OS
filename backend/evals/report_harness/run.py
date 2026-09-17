from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from app.report_harness.test_database import validate_test_dsn
from evals.report_harness.replay import replay_events

ROOT = Path(__file__).resolve().parents[3]
APPROVED = ROOT / "backend/config/report_harness/approved_release_bindings.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def junit_counts(path: Path) -> dict:
    document = ET.parse(path).getroot()
    suites = [document] if document.tag == "testsuite" else list(document.findall("testsuite"))
    counts = {key: sum(int(suite.get(key, "0")) for suite in suites)
              for key in ("tests", "failures", "errors", "skipped")}
    if not suites:
        cases = list(document.findall("testcase"))
        counts = {"tests": len(cases), **{
            name: sum(case.find(element) is not None for case in cases)
            for name, element in (("failures", "failure"), ("errors", "error"),
                                  ("skipped", "skipped"))
        }}
    if not counts["tests"] or any(counts[key] for key in ("failures", "errors", "skipped")):
        raise ValueError(f"关键测试不完整：{counts}")
    return counts


def git(*arguments: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), *arguments], cwd=ROOT, text=True, encoding="utf-8",
    ).strip()


def source_fingerprints() -> dict:
    suffixes = {".py", ".sql", ".md", ".json", ".yaml", ".yml", ".toml",
                ".ts", ".tsx", ".css", ".mjs", ".html", ".svg"}
    paths = {path for directory in ("backend/app", "backend/config", "backend/tests",
                                    "backend/evals", "frontend/src", "frontend/tests")
             for path in (ROOT / directory).rglob("*")
             if path.suffix in suffixes and "__pycache__" not in path.parts}
    paths.update(path for path in (ROOT / "frontend").glob("*")
                 if path.suffix in suffixes)
    paths.update({ROOT / "backend/requirements.txt", ROOT / "pyproject.toml"})
    return {path.relative_to(ROOT).as_posix(): digest(path)
            for path in sorted(paths) if path.is_file()}


def main() -> int:
    parser = argparse.ArgumentParser(description="串行运行完整离线工程门，不调用真实模型")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    validate_test_dsn(os.environ.get("REPORT_HARNESS_TEST_DSN", ""))
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    node = shutil.which("node")
    if not node:
        raise RuntimeError("缺少本机Node，禁止自动下载")
    manifest_path = Path(__file__).with_name("manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for case in manifest["cases"]:
        for relative in case["evidence"]:
            if not (ROOT / relative).is_file():
                raise RuntimeError(f"缺少必需验证：{relative}")
    tags = git("show-ref", "--tags")
    for name, expected in manifest["frozen_tags"].items():
        if git("rev-parse", f"refs/tags/{name}") != expected:
            raise RuntimeError(f"冻结标签变化：{name}")
    approval_digest = digest(APPROVED)
    sources = source_fingerprints()
    result = {
        "started_at": datetime.now(timezone.utc).isoformat(), "commit": git("rev-parse", "HEAD"),
        "git_status": git("status", "--short"), "engineering_gate": "failed",
        "quality_gate": "not_run", "formal_approval_written": False,
        "source_fingerprints": sources, "manifest_digest": digest(manifest_path),
        "tags_before": tags, "commands": [],
    }
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "backend"), "PYTHONIOENCODING": "utf-8",
                   "REPORT_HARNESS_E2E_OUTPUT": str(output / "browser")}
    frontend = ROOT / "frontend"
    commands = [
        ("backend", ROOT, [sys.executable, "-m", "pytest", "backend/tests", "-q",
                          "-p", "no:cacheprovider", "--tb=short",
                          f"--junitxml={output / 'backend.xml'}"]),
        ("frontend-tests", frontend, [node, "node_modules/vitest/vitest.mjs", "run",
                                     "--passWithNoTests=false", "--reporter=junit",
                                     f"--outputFile={output / 'frontend.xml'}"]),
        ("process-tests", frontend, [node, "--test", "--test-reporter=junit",
                                    "tests/process-cleanup.node.mjs"]),
        ("typescript", frontend, [node, "node_modules/typescript/bin/tsc", "-b"]),
        ("frontend-build", frontend, [node, "node_modules/vite/bin/vite.js", "build"]),
        ("browser", frontend, [node, "tests/harness-e2e.mjs"]),
    ]
    try:
        for name, cwd, command in commands:
            started = time.monotonic()
            log = output / f"{name}.log"
            with log.open("w", encoding="utf-8") as stream:
                completed = subprocess.run(command, cwd=cwd, env=environment,
                                           stdout=stream, stderr=subprocess.STDOUT)
            result["commands"].append({
                "name": name, "cwd": str(cwd), "argv": command,
                "exit_code": completed.returncode, "seconds": round(time.monotonic() - started, 3),
                "log_sha256": digest(log),
            })
            print(f"{name}: exit={completed.returncode}", flush=True)
            if completed.returncode:
                raise RuntimeError(f"{name}失败，见本次日志")
        result["backend"] = junit_counts(output / "backend.xml")
        result["frontend"] = junit_counts(output / "frontend.xml")
        result["process_cleanup"] = junit_counts(output / "process-tests.log")
        result["browser"] = json.loads((output / "browser/results.json").read_text(encoding="utf-8"))
        missing = set(manifest["required_browser_cases"]) - set(result["browser"].get("results", []))
        if missing:
            raise RuntimeError(f"浏览器必需场景未验证：{sorted(missing)}")
        result["replay"] = replay_events(json.loads(
            (output / "browser/events.json").read_text(encoding="utf-8")
        ))
        if tags != git("show-ref", "--tags"):
            raise RuntimeError("标签发生变化")
        if approval_digest != digest(APPROVED):
            raise RuntimeError("真实批准表被改写")
        if sources != source_fingerprints():
            raise RuntimeError("验证过程中源码变化，证据不可闭合")
        result["engineering_gate"] = "passed"
    except Exception as exc:
        result["failure"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["artifact_fingerprints"] = {
            path.relative_to(output).as_posix(): digest(path)
            for path in output.rglob("*") if path.is_file() and path.name != "result.json"
        }
        (output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    return 0 if result["engineering_gate"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
