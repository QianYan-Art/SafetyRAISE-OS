from pathlib import Path
from types import SimpleNamespace

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.resources import assert_run_capacity, check_storage, check_linux_memory


def test_low_disk_space_refuses_new_work(monkeypatch):
    monkeypatch.setattr("app.report_harness.resources.shutil.disk_usage",
                        lambda path: SimpleNamespace(free=99))
    with pytest.raises(HarnessError, match="resource_pressure"):
        check_storage((Path("."),), minimum_free_bytes=100)
    check_storage((Path("."),), minimum_free_bytes=99)


def test_storage_probe_failure_is_explicit(monkeypatch):
    def fail(path):
        raise OSError("合成不可访问挂载")

    monkeypatch.setattr("app.report_harness.resources.shutil.disk_usage", fail)
    with pytest.raises(HarnessError, match="resource_probe_failed"):
        check_storage((Path("."),), minimum_free_bytes=100)


def test_concurrency_capacity_is_checked_under_database_lock():
    class Connection:
        def __init__(self, count):
            self.count, self.calls = count, []

        def execute(self, sql):
            self.calls.append(sql)
            return self

        def fetchone(self):
            return {"active_count": self.count}

    connection = Connection(1)
    with pytest.raises(HarnessError, match="server_busy"):
        assert_run_capacity(connection, 1)
    assert "pg_advisory_xact_lock" in connection.calls[0]
    assert "deleted_at IS NULL" in connection.calls[1]
    assert_run_capacity(Connection(0), 1)


def test_container_memory_limit_is_checked_not_just_host_free_memory(tmp_path):
    info = tmp_path / "meminfo"
    info.write_text("MemAvailable: 1048576 kB\n", encoding="ascii")
    (tmp_path / "memory.max").write_text("1000", encoding="ascii")
    (tmp_path / "memory.current").write_text("950", encoding="ascii")
    with pytest.raises(HarnessError, match="resource_pressure"):
        check_linux_memory(minimum_available_bytes=100, proc_meminfo=info, cgroup_root=tmp_path)
    (tmp_path / "memory.max").write_text("max", encoding="ascii")
    check_linux_memory(minimum_available_bytes=100, proc_meminfo=info, cgroup_root=tmp_path)
