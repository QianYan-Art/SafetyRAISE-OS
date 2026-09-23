"""仅供显式本地迁移和测试验证，不读取业务配置。"""

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.report_harness.schema_migrations import apply_pending


def validate_test_dsn(dsn: str) -> str:
    if not dsn:
        raise ValueError("缺少 REPORT_HARNESS_TEST_DSN，禁止回退业务配置。")
    params = conninfo_to_dict(dsn)
    if params.get("host") not in {"127.0.0.1", "localhost"}:
        raise ValueError("测试数据库必须使用回环地址。")
    if params.get("hostaddr", "127.0.0.1") not in {"127.0.0.1", "::1"}:
        raise ValueError("测试数据库 hostaddr 必须使用回环地址。")
    if not params.get("dbname", "").startswith("safetyraise_harness_test"):
        raise ValueError("测试数据库名称不在许可范围。")
    if params.get("service") or params.get("servicefile"):
        raise ValueError("测试数据库禁止间接服务配置。")
    # 显式固定连接地址，防止继承 PGHOSTADDR 等环境设置后绕过回环限制。
    return make_conninfo(dsn, hostaddr="127.0.0.1", connect_timeout="5")


def migrate_test_database(dsn: str) -> None:
    dsn = validate_test_dsn(dsn)
    with psycopg.connect(dsn) as conn, conn.transaction():
        apply_pending(conn)
