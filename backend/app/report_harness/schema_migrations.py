"""报告 harness 的显式数据库迁移清单。

服务启动只核对版本序列，不自动迁移；迁移由操作者经 `python -m app.report_harness.migrate`
显式执行，测试库经 `migrate_test_database` 执行，两者共用这里的清单。
"""

from __future__ import annotations

from pathlib import Path

from psycopg.rows import tuple_row

MIGRATIONS_DIR = Path(__file__).with_name("migrations")
MIGRATIONS = (
    "001_report_runs.sql",
    "002_session_deletion.sql",
    "003_report_feedback.sql",
)
SCHEMA_VERSIONS = tuple(range(1, len(MIGRATIONS) + 1))
_ADVISORY_LOCK = 82341901


def applied_versions(conn) -> set[int]:
    # 自带行格式，不依赖调用方连接的 row_factory。
    with conn.cursor(row_factory=tuple_row) as cursor:
        if cursor.execute("SELECT to_regclass('report_run_schema_version')").fetchone()[0] is None:
            return set()
        return {row[0] for row in cursor.execute("SELECT version FROM report_run_schema_version")}


def apply_pending(conn) -> list[int]:
    """在调用方事务内按序执行未应用的迁移，返回本次应用的版本号。"""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK,))
    applied = applied_versions(conn)
    pending = [version for version in SCHEMA_VERSIONS if version not in applied]
    for version in pending:
        conn.execute((MIGRATIONS_DIR / MIGRATIONS[version - 1]).read_text("utf-8"))
    return pending
