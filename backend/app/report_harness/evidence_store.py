from __future__ import annotations

import json

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.report_harness.errors import HarnessError
from app.report_harness.evidence import EvidenceWriteRequest, audit_records, field_warnings
from app.report_harness.store import RunStore


class EvidenceStore:
    def __init__(self, store: RunStore):
        self.store = store

    def get(self, owner: str, session_id: str) -> dict:
        with self.store.connection() as conn, conn.transaction():
            conn.row_factory = dict_row
            self.store._owned_session(conn, owner, session_id)
            row = conn.execute(
                "SELECT revision, records FROM report_session_evidence "
                "WHERE session_id=%s AND owner_user_id=%s", (session_id, owner),
            ).fetchone()
            return row or {"revision": 0, "records": []}

    def save(self, owner: str, session_id: str, request: EvidenceWriteRequest) -> dict:
        records = audit_records(request.records, owner)
        with self.store.connection() as conn, conn.transaction():
            conn.row_factory = dict_row
            session = self.store._owned_session(conn, owner, session_id)
            existing = conn.execute(
                "SELECT revision, owner_user_id FROM report_session_evidence "
                "WHERE session_id=%s", (session_id,),
            ).fetchone()
            if existing and str(existing["owner_user_id"]) != owner:
                raise HarnessError("not_found", 404)
            revision = existing["revision"] if existing else 0
            if revision != request.expected_revision:
                raise HarnessError("evidence_revision_conflict")
            try:
                draft = json.loads(session["draft_json"])
            except (ValueError, TypeError):
                draft = None
            conn.execute(
                "INSERT INTO report_session_evidence(session_id,owner_user_id,revision,records) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (session_id) DO UPDATE "
                "SET revision=EXCLUDED.revision,records=EXCLUDED.records",
                (session_id, owner, revision + 1, Jsonb(records)),
            )
            return {
                "revision": revision + 1, "records": records,
                "warnings": field_warnings(records, draft),
            }
