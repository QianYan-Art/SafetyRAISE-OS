"""角色最终响应的取证约定：只能引用本轮实际取得原文的来源。

每个候选轮次都会清空原文访问记录，修订稿沿用上一版引用时必须重新读取。检查不通过时
抛出 ResponseRejected，由角色循环逐项反馈给模型补读或删去引用；约定本身不放宽，修复
次数用尽仍未满足时运行以 invalid_role_response 停止。
"""

from __future__ import annotations

from collections.abc import Iterable

from app.report_harness.contracts import CandidateReport
from app.report_harness.role_loop import ResponseRejected

UNREAD_SOURCE = "source_not_read"

_EVIDENCE_HINT = "本轮尚未读取该事实原文；先用read_evidence读取，或删去该引用。"
_KNOWLEDGE_HINT = "本轮尚未完整读取该知识原文；先用read_knowledge读完（含后续分页），或删去该引用。"
_SOURCE_HINT = "本轮尚未取得该来源原文；先读取，或删去该引用。"
_REQUIRED_EVIDENCE_HINT = "审查必需的事实原文尚未读取；先用read_evidence读取。"
_CANDIDATE_KNOWLEDGE_HINT = "候选引用的知识原文尚未完整读取；先用read_knowledge读完（含后续分页）。"


def _unread(loc: tuple, refs: Iterable[str], available: set[str],
            hint: str) -> list[tuple[tuple, str]]:
    return [(loc, f"{ref}：{hint}") for ref in sorted(set(refs) - available)]


def check_generator_access(candidate: CandidateReport, evidence: set[str],
                           knowledge: set[str]) -> None:
    """断言与问题回应引用的事实、知识都已在本轮取得（知识须完整读完）。"""
    problems = []
    for index, claim in enumerate(candidate.claims):
        problems += _unread(("claims", index, "evidence_refs"),
                            claim.evidence_refs, evidence, _EVIDENCE_HINT)
        problems += _unread(("claims", index, "knowledge_refs"),
                            claim.knowledge_refs, knowledge, _KNOWLEDGE_HINT)
    for index, response in enumerate(candidate.issue_responses):
        problems += _unread(("issue_responses", index, "source_refs"),
                            response.source_refs, evidence | knowledge, _SOURCE_HINT)
    if problems:
        raise ResponseRejected(UNREAD_SOURCE, problems)


def check_reviewer_access(required_evidence: set[str], candidate_knowledge: set[str],
                          evidence: set[str], knowledge: set[str]) -> None:
    """审查者须取得全部必要事实原文，并完整读取候选引用的每条知识。"""
    problems = (_unread((), required_evidence, evidence, _REQUIRED_EVIDENCE_HINT)
                + _unread((), candidate_knowledge, knowledge, _CANDIDATE_KNOWLEDGE_HINT))
    if problems:
        raise ResponseRejected(UNREAD_SOURCE, problems)
