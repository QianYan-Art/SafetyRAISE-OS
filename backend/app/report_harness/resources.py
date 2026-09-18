from pathlib import Path
import os
import shutil

from app.report_harness.errors import HarnessError


def check_storage(paths: tuple[Path, ...], *, minimum_free_bytes: int) -> None:
    if not paths or type(minimum_free_bytes) is not int or minimum_free_bytes <= 0:
        raise ValueError("必须明确检查目录与保留空间。")
    for path in paths:
        try:
            free = shutil.disk_usage(path).free
        except OSError as exc:
            raise HarnessError("resource_probe_failed", 503) from exc
        if free < minimum_free_bytes:
            raise HarnessError("resource_pressure", 503, {"resource": "disk"})


def check_linux_memory(*, minimum_available_bytes: int,
                       proc_meminfo: Path = Path("/proc/meminfo"),
                       cgroup_root: Path = Path("/sys/fs/cgroup")) -> None:
    if type(minimum_available_bytes) is not int or minimum_available_bytes <= 0:
        raise ValueError("必须明确内存保留量。")
    try:
        fields = dict(line.split(":", 1) for line in proc_meminfo.read_text().splitlines() if ":" in line)
        amount, unit = fields["MemAvailable"].split()
        if unit != "kB":
            raise ValueError("未知内存计量单位。")
        available = int(amount) * 1024
        maximum_path = cgroup_root / "memory.max"
        if maximum_path.exists():
            maximum = maximum_path.read_text().strip()
            if maximum != "max":
                available = min(
                    available, int(maximum) - int((cgroup_root / "memory.current").read_text()),
                )
    except (OSError, KeyError, ValueError) as exc:
        raise HarnessError("resource_probe_failed", 503) from exc
    if available < minimum_available_bytes:
        raise HarnessError("resource_pressure", 503, {"resource": "memory"})


def check_host_memory(*, minimum_available_bytes: int) -> None:
    if os.name != "nt":
        return check_linux_memory(minimum_available_bytes=minimum_available_bytes)
    if type(minimum_available_bytes) is not int or minimum_available_bytes <= 0:
        raise ValueError("必须明确内存保留量。")
    import ctypes
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD), ("load", wintypes.DWORD),
            ("total_physical", ctypes.c_ulonglong), ("available_physical", ctypes.c_ulonglong),
            ("total_pagefile", ctypes.c_ulonglong), ("available_pagefile", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    query = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
    query.argtypes = [ctypes.POINTER(MemoryStatus)]
    query.restype = wintypes.BOOL
    if not query(ctypes.byref(status)):
        raise HarnessError("resource_probe_failed", 503)
    if status.available_physical < minimum_available_bytes:
        raise HarnessError("resource_pressure", 503, {"resource": "memory"})


def assert_run_capacity(conn, maximum: int | None) -> None:
    """事务锁串行检查全局活动运行数，不依赖单进程信号量。"""
    if maximum is None:
        return
    if type(maximum) is not int or maximum < 1:
        raise ValueError("活动运行容量必须为正整数。")
    conn.execute("SELECT pg_advisory_xact_lock(1953654130, 1)")
    row = conn.execute(
        "SELECT count(*) AS active_count FROM report_runs "
        "WHERE state IN ('preparing','generating','checking','revising') "
        "AND deleted_at IS NULL"
    ).fetchone()
    count = row["active_count"] if isinstance(row, dict) else row[0]
    if count >= maximum:
        raise HarnessError("server_busy", 409, {"maximum_active_runs": maximum})
