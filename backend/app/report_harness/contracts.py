from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.schemas.report_run import CandidateReport, ReviewResult


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_CHECK_CATEGORIES = frozenset(
    {"facts", "coverage", "reasoning", "citations", "conciseness"}
)
_SEMANTIC_ISSUE_CATEGORIES = frozenset(
    {"fact", "facts", "inference", "reasoning", "citation", "citations", "事实", "推理", "引用"}
)
_UNRESOLVED_SEVERITIES = frozenset({"blocker", "major"})
_UNRESOLVED_STATUSES = frozenset({"open", "contested"})


def canonical_digest(value: Any) -> str:
    """按固定 JSON 编码计算 SHA-256 摘要。"""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    else:
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            value = model_dump(mode="json")
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("值不能编码为规范 JSON。") from exc
    return hashlib.sha256(payload).hexdigest()


def _normalize_ids(values: Iterable[object], name: str) -> set[str]:
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, (str, UUID)):
            raise ValueError(f"{name} 只能包含字符串或 UUID。")
        text = str(value).strip()
        if not text:
            raise ValueError(f"{name} 不能包含空 ID。")
        normalized.add(text)
    return normalized


def _require_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} 必须是 64 位小写 SHA-256 摘要。")
    return value


def _validate_split_refs(
    evidence_refs: Iterable[str],
    knowledge_refs: Iterable[str],
    evidence_ids: set[str],
    knowledge_ids: set[str],
    location: str,
) -> None:
    for ref in evidence_refs:
        if ref not in evidence_ids:
            raise ValueError(f"{location} 包含未授权的 evidence 引用：{ref}。")
    for ref in knowledge_refs:
        if ref not in knowledge_ids:
            raise ValueError(f"{location} 包含未授权的 knowledge 引用：{ref}。")


def _validate_mixed_refs(
    refs: Iterable[str],
    evidence_ids: set[str],
    knowledge_ids: set[str],
    location: str,
) -> None:
    allowed = evidence_ids | knowledge_ids
    for ref in refs:
        if ref not in allowed:
            raise ValueError(f"{location} 包含越界来源引用：{ref}。")


def _validate_report_contract(
    candidate: CandidateReport,
    review: ReviewResult,
    snapshot_digest: str,
    obligation_ids: Iterable[object],
    evidence_ids: Iterable[object],
    knowledge_ids: Iterable[object],
    *,
    require_passed: bool,
    resolved_issue_ids: frozenset[str],
) -> None:
    """执行发布前的确定性检查；任何失败均以 ValueError 拒绝发布。"""
    if not isinstance(candidate, CandidateReport):
        raise ValueError("candidate 必须是 CandidateReport。")
    if not isinstance(review, ReviewResult):
        raise ValueError("review 必须是 ReviewResult。")

    snapshot_digest = _require_sha256(snapshot_digest, "snapshot_digest")
    candidate_payload = candidate.model_dump(mode="json")
    candidate_digest = _require_sha256(
        canonical_digest(candidate_payload), "candidate_digest"
    )
    if review.candidate_digest != candidate_digest:
        raise ValueError("candidate_digest 与最终候选正文不匹配。")
    if review.snapshot_digest != snapshot_digest:
        raise ValueError("snapshot_digest 与冻结快照不匹配。")

    required_obligations = _normalize_ids(obligation_ids, "obligation_ids")
    allowed_evidence = _normalize_ids(evidence_ids, "evidence_ids")
    allowed_knowledge = _normalize_ids(knowledge_ids, "knowledge_ids")

    if not candidate.report_markdown.strip():
        raise ValueError("候选正文不能为空。")
    if not candidate.claims:
        raise ValueError("候选必须包含至少一个可追溯断言。")

    claim_ids: set[str] = set()
    for claim in candidate.claims:
        if claim.claim_id in claim_ids:
            raise ValueError(f"候选包含重复 claim_id：{claim.claim_id}。")
        claim_ids.add(claim.claim_id)
        if claim.text_span.end > len(candidate.report_markdown):
            raise ValueError(f"断言 {claim.claim_id} 的正文定位超出候选正文范围。")
        if not candidate.report_markdown[claim.text_span.start:claim.text_span.end].strip():
            raise ValueError(f"断言 {claim.claim_id} 的正文定位没有覆盖正文内容。")
        if not claim.evidence_refs and not claim.knowledge_refs:
            raise ValueError(f"断言 {claim.claim_id} 缺少来源引用。")
        if claim.type == "knowledge" and not claim.knowledge_refs:
            raise ValueError(f"knowledge 断言 {claim.claim_id} 必须引用 knowledge。")
        _validate_split_refs(
            claim.evidence_refs,
            claim.knowledge_refs,
            allowed_evidence,
            allowed_knowledge,
            f"断言 {claim.claim_id}",
        )

    seen_obligations: set[str] = set()
    for item in candidate.obligation_resolutions:
        if item.obligation_id in seen_obligations:
            raise ValueError(f"候选包含重复 obligation_id：{item.obligation_id}。")
        if item.obligation_id not in required_obligations:
            raise ValueError(f"候选包含未声明义务：{item.obligation_id}。")
        seen_obligations.add(item.obligation_id)
        if not item.resolution.strip():
            raise ValueError(f"义务 {item.obligation_id} 缺少处置说明。")
    missing_obligations = required_obligations - seen_obligations
    if missing_obligations:
        raise ValueError(f"候选缺少义务处置：{sorted(missing_obligations)}。")

    seen_coverage_obligations: set[str] = set()
    for check in review.coverage_checks:
        if check.obligation_id in seen_coverage_obligations:
            raise ValueError(f"审查包含重复 coverage obligation_id：{check.obligation_id}。")
        if check.obligation_id not in required_obligations:
            raise ValueError(f"coverage 检查包含未声明义务：{check.obligation_id}。")
        seen_coverage_obligations.add(check.obligation_id)
        if require_passed and not check.passed:
            raise ValueError(f"义务 {check.obligation_id} 的 coverage 检查未通过。")
        if not check.conclusion.strip():
            raise ValueError(f"义务 {check.obligation_id} 的 coverage 检查缺少结论。")
        for claim_id in check.claim_ids:
            if claim_id not in claim_ids:
                raise ValueError(f"coverage 检查引用了不存在的 claim_id：{claim_id}。")
        if not check.claim_ids and not check.evidence_refs and not check.knowledge_refs:
            raise ValueError(f"义务 {check.obligation_id} 的 coverage 检查缺少具体依据。")
        _validate_split_refs(
            check.evidence_refs,
            check.knowledge_refs,
            allowed_evidence,
            allowed_knowledge,
            f"义务 {check.obligation_id} 的 coverage 检查",
        )
    if seen_coverage_obligations != required_obligations:
        missing = required_obligations - seen_coverage_obligations
        extra = seen_coverage_obligations - required_obligations
        details = []
        if missing:
            details.append(f"缺少 {sorted(missing)}")
        if extra:
            details.append(f"包含非法 {sorted(extra)}")
        raise ValueError("义务 coverage 检查不完整：" + "；".join(details) + "。")

    seen_categories: set[str] = set()
    for check in review.completed_checks:
        if check.category in seen_categories:
            raise ValueError(f"审查重复完成检查类别：{check.category}。")
        seen_categories.add(check.category)
        if require_passed and not check.passed:
            raise ValueError(f"审查检查未通过：{check.category}。")
        if not check.conclusion.strip():
            raise ValueError(f"审查检查缺少具体结论：{check.category}。")
        if not check.evidence_refs and not check.knowledge_refs:
            raise ValueError(f"审查检查缺少具体来源依据：{check.category}。")
        _validate_split_refs(
            check.evidence_refs,
            check.knowledge_refs,
            allowed_evidence,
            allowed_knowledge,
            f"审查检查 {check.category}",
        )
    if seen_categories != _REQUIRED_CHECK_CATEGORIES:
        missing = _REQUIRED_CHECK_CATEGORIES - seen_categories
        extra = seen_categories - _REQUIRED_CHECK_CATEGORIES
        details = []
        if missing:
            details.append(f"缺少 {sorted(missing)}")
        if extra:
            details.append(f"包含非法 {sorted(extra)}")
        raise ValueError("五类审查检查不完整：" + "；".join(details) + "。")

    seen_issues: set[str] = set()
    for issue in review.issues:
        if issue.issue_id in seen_issues:
            raise ValueError(f"审查包含重复 issue_id：{issue.issue_id}。")
        seen_issues.add(issue.issue_id)
        _validate_mixed_refs(
            issue.source_refs,
            allowed_evidence,
            allowed_knowledge,
            f"问题 {issue.issue_id}",
        )
        if issue.category.strip().lower() in _SEMANTIC_ISSUE_CATEGORIES and issue.severity == "minor":
            raise ValueError("事实、推理或引用问题不能标记为 minor。")
        if issue.severity == "minor" and issue.category.strip().lower() not in {
            "style", "formatting", "wording",
        }:
            raise ValueError("只有明确的排版或措辞问题可以标记为 minor。")
        if issue.status == "resolved" and issue.issue_id not in resolved_issue_ids:
            raise ValueError("当前契约没有历史问题闭合上下文，不接受模型首次声明 resolved。")
        if (require_passed and issue.severity in _UNRESOLVED_SEVERITIES
                and issue.status in _UNRESOLVED_STATUSES):
            raise ValueError(
                f"仍有未关闭的 {issue.severity} 问题：{issue.issue_id}。"
            )


def validate_publication(
    candidate: CandidateReport, review: ReviewResult, snapshot_digest: str,
    obligation_ids: Iterable[object], evidence_ids: Iterable[object],
    knowledge_ids: Iterable[object], *, resolved_issue_ids: frozenset[str] = frozenset(),
) -> None:
    """发布必须通过所有检查；闭合问题只能来自控制器的历史台账。"""
    _validate_report_contract(
        candidate, review, snapshot_digest, obligation_ids, evidence_ids, knowledge_ids,
        require_passed=True, resolved_issue_ids=resolved_issue_ids,
    )


def validate_review_structure(
    candidate: CandidateReport, review: ReviewResult, snapshot_digest: str,
    obligation_ids: Iterable[object], evidence_ids: Iterable[object],
    knowledge_ids: Iterable[object], *, resolved_issue_ids: frozenset[str] = frozenset(),
) -> None:
    """允许有依据的负面审查进入修订，不放宽摘要、引用或五类检查契约。"""
    _validate_report_contract(
        candidate, review, snapshot_digest, obligation_ids, evidence_ids, knowledge_ids,
        require_passed=False, resolved_issue_ids=resolved_issue_ids,
    )
