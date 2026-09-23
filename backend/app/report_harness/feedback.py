"""报告质量反馈：用户对自己的报告运行填写、修改意见；管理员查看汇总并导出。

每次保存追加一个修订并冻结当时的运行状态与版本摘要，意见始终能对应到具体报告与系统版本。
"""

from __future__ import annotations

import csv
import io
from typing import Literal

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import Field, field_validator

from app.report_harness.errors import HarnessError
from app.report_harness.store import RunStore
from app.schemas.base import StrictModel

FeedbackVerdict = Literal["usable", "needs_revision", "unusable"]
FeedbackTag = Literal[
    "fact_error", "missing_fact", "liability", "legal_citation", "reasoning", "format", "other",
]
VERDICT_LABELS = {"usable": "可直接使用", "needs_revision": "修改后可用", "unusable": "不可用"}
TAG_LABELS = {
    "fact_error": "事实错误", "missing_fact": "遗漏关键事实", "liability": "责任认定不当",
    "legal_citation": "法规引用错误或过时", "reasoning": "推理不清", "format": "表述格式",
    "other": "其他",
}


class FeedbackWriteRequest(StrictModel):
    expected_revision: int = Field(ge=0)
    reviewer_name: str = Field(min_length=1, max_length=40)
    verdict: FeedbackVerdict
    issue_tags: list[FeedbackTag] = Field(default_factory=list, max_length=len(TAG_LABELS))
    comment: str = Field(default="", max_length=8000)

    @field_validator("reviewer_name", "comment")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("reviewer_name")
    @classmethod
    def _require_name(cls, value: str) -> str:
        if not value:
            raise ValueError("反馈人不能为空。")
        return value

    @field_validator("issue_tags")
    @classmethod
    def _unique_tags(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("问题类型不得重复。")
        return value


def _public(row: dict) -> dict:
    return {
        "run_id": str(row["run_id"]), "revision": row["revision"],
        "reviewer_name": row["reviewer_name"], "verdict": row["verdict"],
        "issue_tags": list(row["issue_tags"]), "comment": row["comment"],
        "updated_at": row["created_at"].isoformat(),
    }


def _run_context(run: dict, session_title: str | None) -> dict:
    document = run["document"]
    return {
        "session_id": run["session_id"], "session_title": session_title,
        "run_created_at": run["created_at"].isoformat(), "state": run["state"],
        "review_status": document.get("review_status"),
        "terminal_reason": document.get("terminal_reason"),
        "candidate_version": document.get("candidate_version"),
        "quality_gate": document.get("quality_gate"),
        "endpoint_profile_digest": document.get("endpoint_profile_digest"),
        "policy_digest": document.get("policy_digest"),
        "knowledge_manifest_digest": document.get("snapshot", {}).get("knowledge_manifest_digest"),
        "release_binding": document.get("release_binding"),
    }


_LATEST = (
    "SELECT DISTINCT ON (f.run_id) f.*, u.username AS author_username "
    "FROM report_run_feedback f JOIN users u ON u.id=f.author_user_id "
    "ORDER BY f.run_id, f.revision DESC"
)


class FeedbackStore:
    def __init__(self, store: RunStore):
        self.store = store

    def get(self, owner: str, run_id: str) -> dict:
        with self.store.connection() as conn, conn.transaction():
            conn.row_factory = dict_row
            self._owned_run(conn, owner, run_id)
            row = conn.execute(
                "SELECT * FROM report_run_feedback WHERE run_id=%s ORDER BY revision DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return _public(row) if row else {"run_id": run_id, "revision": 0}

    def save(self, owner: str, run_id: str, request: FeedbackWriteRequest) -> dict:
        try:
            with self.store.connection() as conn, conn.transaction():
                conn.row_factory = dict_row
                run = self._owned_run(conn, owner, run_id)
                current = conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) AS revision FROM report_run_feedback "
                    "WHERE run_id=%s", (run_id,),
                ).fetchone()["revision"]
                if current != request.expected_revision:
                    raise HarnessError("feedback_revision_conflict")
                title = conn.execute(
                    "SELECT title FROM chat_sessions WHERE id=%s", (run["session_id"],),
                ).fetchone()
                row = conn.execute(
                    "INSERT INTO report_run_feedback(run_id,revision,author_user_id,reviewer_name,"
                    "verdict,issue_tags,comment,run_context) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                    "RETURNING *",
                    (run_id, current + 1, owner, request.reviewer_name, request.verdict,
                     request.issue_tags, request.comment,
                     Jsonb(_run_context(run, title["title"] if title else None))),
                ).fetchone()
        except UniqueViolation:
            # 并发保存同一修订号时只有一方成功，另一方按版本冲突处理。
            raise HarnessError("feedback_revision_conflict") from None
        return _public(row)

    def list_latest(self, *, verdict: str | None = None, tag: str | None = None,
                    limit: int = 50, offset: int = 0) -> dict:
        where, params = self._filters(verdict, tag)
        with self.store.connection() as conn:
            conn.row_factory = dict_row
            total = conn.execute(
                f"SELECT count(*) AS n FROM ({_LATEST}) latest {where}", params,
            ).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * FROM ({_LATEST}) latest {where} "
                "ORDER BY created_at DESC LIMIT %s OFFSET %s", (*params, limit, offset),
            ).fetchall()
        return {"total": total, "items": [self._admin_view(row) for row in rows]}

    def export_csv(self, *, verdict: str | None = None, tag: str | None = None) -> bytes:
        where, params = self._filters(verdict, tag)
        with self.store.connection() as conn:
            conn.row_factory = dict_row
            rows = conn.execute(
                f"SELECT * FROM ({_LATEST}) latest {where} ORDER BY created_at DESC", params,
            ).fetchall()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "反馈时间", "反馈人", "账号", "报告编号", "事故会话", "运行状态", "质量资格",
            "总体结论", "问题类型", "具体意见", "修订次数", "运行创建时间",
            "模型端点摘要", "知识库摘要", "策略摘要",
        ])
        for row in rows:
            item = self._admin_view(row)
            context = item["run_context"]
            writer.writerow([
                item["updated_at"], item["reviewer_name"], item["author_username"], item["run_id"],
                context.get("session_title") or "", context.get("state") or "",
                context.get("quality_gate") or "", VERDICT_LABELS[item["verdict"]],
                "、".join(TAG_LABELS[tag] for tag in item["issue_tags"]), item["comment"],
                item["revision"], context.get("run_created_at") or "",
                context.get("endpoint_profile_digest") or "",
                context.get("knowledge_manifest_digest") or "", context.get("policy_digest") or "",
            ])
        # 带 BOM 的 UTF-8，Excel 直接打开不乱码。
        return ("\ufeff" + buffer.getvalue()).encode("utf-8")

    @staticmethod
    def _owned_run(conn, owner: str, run_id: str) -> dict:
        run = conn.execute(
            "SELECT run_id, session_id, state, created_at, document FROM report_runs "
            "WHERE run_id=%s AND owner_user_id=%s AND deleted_at IS NULL", (run_id, owner),
        ).fetchone()
        if run is None:
            raise HarnessError("not_found", 404)
        return run

    @staticmethod
    def _filters(verdict: str | None, tag: str | None) -> tuple[str, tuple]:
        clauses, params = [], []
        if verdict is not None:
            clauses.append("verdict=%s")
            params.append(verdict)
        if tag is not None:
            clauses.append("%s = ANY(issue_tags)")
            params.append(tag)
        return ("WHERE " + " AND ".join(clauses) if clauses else ""), tuple(params)

    @staticmethod
    def _admin_view(row: dict) -> dict:
        return {**_public(row), "author_username": row["author_username"],
                "run_context": row["run_context"]}
