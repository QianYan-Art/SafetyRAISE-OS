import socket
from pathlib import Path

import pytest

from evals.report_harness.replay import replay_events
from evals.report_harness.run import junit_counts


def test_record_replay_is_local_and_does_not_send_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("只读重放不得发网络请求")

    monkeypatch.setattr(socket, "socket", forbidden)
    events = [
        {"run_id": "fixture", "seq": 1, "state_version": 1, "type": "stage",
         "data": {"state": "preparing"}},
        {"run_id": "fixture", "seq": 2, "state_version": 2, "type": "final",
         "data": {"state": "published", "budget": {"physical_requests": 2, "known_used": 400}}},
    ]
    result = replay_events(events)
    assert result["observed_state"] == "published"
    assert result["publication_budget"]["physical_requests"] == 2
    assert result["quality_gate"] == "engineering_only"
    assert events[0]["data"]["state"] == "preparing"
    with pytest.raises(ValueError):
        replay_events([events[1]])


@pytest.mark.parametrize("attribute", ["failures", "errors", "skipped"])
def test_engineering_runner_rejects_non_passing_or_skipped_tests(tmp_path, attribute):
    path = tmp_path / "result.xml"
    path.write_text(f'<testsuites><testsuite tests="2" {attribute}="1"/></testsuites>',
                    encoding="utf-8")
    with pytest.raises(ValueError, match="关键测试不完整"):
        junit_counts(path)


def test_engineering_runner_accepts_nonempty_complete_report(tmp_path):
    path = tmp_path / "result.xml"
    path.write_text('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0"/></testsuites>',
                    encoding="utf-8")
    assert junit_counts(path)["tests"] == 2


@pytest.mark.parametrize("problem", ["", "failure", "error", "skipped"])
def test_engineering_runner_checks_node_junit_testcases(tmp_path, problem):
    path = tmp_path / "node.xml"
    details = f"<{problem}/>" if problem else ""
    path.write_text(f'<testsuites><testcase name="cleanup">{details}</testcase></testsuites>',
                    encoding="utf-8")
    if problem:
        with pytest.raises(ValueError):
            junit_counts(path)
    else:
        assert junit_counts(path)["tests"] == 1


def test_source_fingerprints_cover_execution_and_build_contracts(tmp_path, monkeypatch):
    from evals.report_harness import run

    monkeypatch.setattr(run, "ROOT", tmp_path)
    paths = ["backend/app/report_harness/migrations/001.sql",
             "backend/config/report_harness/generator.md",
             "backend/config/workflow.yaml", "frontend/vite.config.ts",
             "frontend/tsconfig.json", "frontend/package-lock.json",
             "backend/requirements.txt"]
    for relative in paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("原始合成契约", encoding="utf-8")
    secret_paths = {tmp_path / relative for relative in
                    ("backend/.env", "backend/config/.env", "frontend/.env",
                     "frontend/src/.env.local")}
    for path in secret_paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("不读取业务配置", encoding="utf-8")
    read_bytes, read_text = Path.read_bytes, Path.read_text

    def guarded_read_bytes(path, *args, **kwargs):
        assert path not in secret_paths, "指纹扫描不得读取业务配置"
        return read_bytes(path, *args, **kwargs)

    def guarded_read_text(path, *args, **kwargs):
        assert path not in secret_paths, "指纹扫描不得读取业务配置"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    before = run.source_fingerprints()
    assert set(before) == set(paths)
    (tmp_path / paths[0]).write_text("变化后的合成契约", encoding="utf-8")
    assert run.source_fingerprints() != before


@pytest.mark.parametrize("failure", ["prepare", "database", "insert", "close"])
def test_browser_server_startup_failures_release_registered_resources(monkeypatch, failure):
    from contextlib import contextmanager
    from tests import harness_dev_server as server

    events = []
    original_connect = socket.socket.connect
    monkeypatch.setenv("REPORT_HARNESS_TEST_DSN",
                       "host=127.0.0.1 dbname=safetyraise_harness_test user=postgres")
    monkeypatch.setattr(server, "make_settings", lambda *_, **__: None)

    def prepare(_dsn):
        if failure == "prepare":
            raise RuntimeError("合成初始化失败")

    class Database:
        def __init__(self, _settings):
            if failure == "database":
                raise RuntimeError("合成建池失败")
            self._pool = self

        @contextmanager
        def connection(self):
            yield self

        def execute(self, _sql, _params):
            events.append("insert")
            if events.count("insert") == 2:
                raise RuntimeError("合成写入失败")

        def close(self):
            events.append("close")
            if failure == "close":
                raise RuntimeError("合成关闭失败")

    monkeypatch.setattr(server, "prepare_tables", prepare)
    monkeypatch.setattr(server, "DatabaseService", Database)
    monkeypatch.setattr(server, "RunStore", lambda *_: object())
    monkeypatch.setattr(server, "cleanup", lambda *_: events.append("cleanup"))
    with pytest.raises(RuntimeError, match="合成"):
        server.main()
    assert socket.socket.connect is original_connect
    if failure == "prepare":
        assert events == []
    elif failure == "database":
        assert events == ["cleanup"]
    else:
        assert events == ["insert", "insert", "close", "cleanup"]
