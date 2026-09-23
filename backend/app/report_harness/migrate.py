"""显式执行报告 harness 的数据库迁移。

    python -m app.report_harness.migrate            # 只列出待执行版本
    python -m app.report_harness.migrate --confirm  # 实际执行

连接业务库沿用主应用配置中的 `database.dsn`；服务启动时从不自动迁移。
"""

from __future__ import annotations

import argparse
import sys

import psycopg

from app.core.settings import load_settings
from app.report_harness.schema_migrations import SCHEMA_VERSIONS, applied_versions, apply_pending


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行报告 harness 数据库迁移。")
    parser.add_argument("--config", default=None, help="工作流配置路径，缺省同主应用。")
    parser.add_argument("--confirm", action="store_true", help="实际执行；缺省只列出待执行版本。")
    args = parser.parse_args(argv)
    dsn = load_settings(args.config).database.dsn
    with psycopg.connect(dsn, connect_timeout=10) as conn, conn.transaction():
        if not args.confirm:
            pending = [version for version in SCHEMA_VERSIONS if version not in applied_versions(conn)]
            print(f"待执行迁移：{pending or '无'}")
            return 0
        applied = apply_pending(conn)
    print(f"已执行迁移：{applied or '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
