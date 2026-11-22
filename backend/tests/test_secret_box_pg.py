from __future__ import annotations

from app.core.secret_box import (
    PGCRYPTO_PREFIX,
    pgcrypto_decrypt_sql,
    pgcrypto_encrypt_params,
    pgcrypto_encrypt_sql,
)


def test_pgcrypto_encrypt_and_decrypt_roundtrip_on_real_postgres(pg_store):
    store, _, _, _ = pg_store
    enc_key = "test-derived-key-" + "a1b2c3d4e5f6"
    sample_plain = "sample-user-credential-" + "9876543210"

    with store.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

            # 1. 非空明文加密后带 pgcrypto:v1: 前缀且不含原文，再解密与原文一致
            cur.execute(
                f"SELECT {pgcrypto_encrypt_sql()} AS encrypted",
                pgcrypto_encrypt_params(sample_plain, enc_key),
            )
            encrypted = cur.fetchone()["encrypted"]
            assert isinstance(encrypted, str)
            assert encrypted.startswith(PGCRYPTO_PREFIX)
            assert sample_plain not in encrypted

            cur.execute(
                f"SELECT {pgcrypto_decrypt_sql('val')} FROM (VALUES (%s::text)) AS t(val)",
                (enc_key, encrypted),
            )
            assert cur.fetchone()["val"] == sample_plain

            # 2. None 与空串加密结果为 NULL，不触发 IndeterminateDatatype
            for empty_val in (None, "", "   "):
                cur.execute(
                    f"SELECT {pgcrypto_encrypt_sql()} AS encrypted",
                    pgcrypto_encrypt_params(empty_val, enc_key),
                )
                assert cur.fetchone()["encrypted"] is None

            # 3. 历史明文与 NULL 值兼容读取
            cur.execute(
                f"SELECT {pgcrypto_decrypt_sql('val')} FROM (VALUES (%s::text)) AS t(val)",
                (enc_key, "legacy-plaintext-val"),
            )
            assert cur.fetchone()["val"] == "legacy-plaintext-val"

            cur.execute(
                f"SELECT {pgcrypto_decrypt_sql('val')} FROM (VALUES (NULL::text)) AS t(val)",
                (enc_key,),
            )
            assert cur.fetchone()["val"] is None
