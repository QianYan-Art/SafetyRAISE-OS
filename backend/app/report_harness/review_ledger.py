from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4, uuid5

from app.schemas.report_run import IssueResponse, ReviewResult


_UNRESOLVED_STATUSES = frozenset({"open", "contested"})
_LOCKED_FIELDS = ("category", "severity", "target", "closure_condition")
_IDENTITY_FIELDS = ("category", "severity", "target", "closure_condition")


class IssueLedger:
    """维护审查问题的稳定程序 ID、生命周期和追加式历史。"""

    def __init__(self, namespace: str | None = None) -> None:
        self._current: dict[str, dict[str, Any]] = {}
        self._history: list[dict[str, Any]] = []
        self._namespace = UUID(namespace) if namespace else None
        self._allocated = 0

    def apply(self, review: ReviewResult) -> ReviewResult:
        """校验并应用一次审查结果，返回带程序 ID 的严格副本。"""
        if not isinstance(review, ReviewResult):
            raise ValueError("review 必须是 ReviewResult 实例。")

        payload = review.model_dump(mode="json")
        issues = payload["issues"]
        incoming_ids = [issue["issue_id"] for issue in issues]
        if len(set(incoming_ids)) != len(incoming_ids):
            raise ValueError("同一审查结果不能包含重复 issue_id。")

        if not self._history:
            staged = self._stage_initial(issues)
        else:
            staged = self._stage_follow_up(issues, incoming_ids)

        output_payload = deepcopy(payload)
        output_payload["issues"] = deepcopy(staged)
        try:
            result = ReviewResult.model_validate(output_payload)
        except ValueError as exc:
            raise ValueError("应用审查结果时无法重建严格 ReviewResult。") from exc

        for issue in staged:
            issue_id = issue["issue_id"]
            self._current[issue_id] = deepcopy(issue)
            self._history.append(deepcopy(issue))
        return result

    def unresolved(self) -> list[dict]:
        """返回当前尚未关闭的问题副本，包含 blocker、major 和 minor。"""
        return [
            deepcopy(issue)
            for issue in self._current.values()
            if issue["status"] in _UNRESOLVED_STATUSES
        ]

    def history(self) -> list[dict]:
        """返回按应用顺序排列的不可变历史快照副本。"""
        return deepcopy(self._history)

    def resolved_ids(self) -> set[str]:
        """返回当前状态为 resolved 且已进入历史的稳定程序 ID。"""
        history_ids = {issue["issue_id"] for issue in self._history}
        return {
            issue_id
            for issue_id, issue in self._current.items()
            if issue_id in history_ids and issue["status"] == "resolved"
        }

    def record_responses(self, responses: list[IssueResponse]) -> None:
        """记录生成者对当前未关闭问题的可核查回应。"""
        if not isinstance(responses, list):
            raise ValueError("responses 必须是 IssueResponse 列表。")

        staged: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        seen: set[str] = set()
        for response in responses:
            if not isinstance(response, IssueResponse):
                raise ValueError("回应必须是 IssueResponse 实例。")
            data = response.model_dump(mode="json")
            issue_id = data["issue_id"]
            if issue_id in seen:
                raise ValueError("同一批回应不能重复 issue_id。")
            seen.add(issue_id)

            previous = self._current.get(issue_id)
            if previous is None:
                raise ValueError("回应只能引用当前台账中的问题。")
            if previous["status"] not in _UNRESOLVED_STATUSES:
                raise ValueError("回应只能引用当前未关闭的问题。")
            if data["action"] not in {"revised", "contested"}:
                raise ValueError("回应 action 只能是 revised 或 contested。")

            updated = deepcopy(previous)
            if data["action"] == "contested":
                updated["status"] = "contested"
            record = {
                "record_type": "issue_response",
                "role": "generator",
                "issue_id": issue_id,
                "action": data["action"],
                "explanation": data["explanation"],
                "source_refs": deepcopy(data["source_refs"]),
            }
            staged.append((issue_id, updated, record))

        for issue_id, updated, record in staged:
            self._current[issue_id] = updated
            self._history.append(record)

    def _stage_initial(self, issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
        staged: list[dict[str, Any]] = []
        reserved: set[str] = set()
        for issue in issues:
            if issue["status"] == "resolved":
                raise ValueError("首次出现的问题不能直接标记为 resolved。")
            assigned = deepcopy(issue)
            assigned["issue_id"] = self._new_id(reserved)
            reserved.add(assigned["issue_id"])
            staged.append(assigned)
        return staged

    def _stage_follow_up(
        self,
        issues: list[dict[str, Any]],
        incoming_ids: list[str],
    ) -> list[dict[str, Any]]:
        incoming_id_set = set(incoming_ids)
        missing = [
            issue_id
            for issue_id, issue in self._current.items()
            if issue["status"] in _UNRESOLVED_STATUSES and issue_id not in incoming_id_set
        ]
        if missing:
            raise ValueError(
                "后续审查必须显式保留所有未关闭问题：" + ", ".join(missing),
            )

        staged: list[dict[str, Any]] = []
        reserved: set[str] = set()
        for issue in issues:
            issue_id = issue["issue_id"]
            previous = self._current.get(issue_id)
            if previous is not None:
                self._validate_known_issue(previous, issue)
                updated = deepcopy(issue)
                if updated["status"] == "resolved":
                    self._validate_resolution(updated)
                staged.append(updated)
                continue

            if issue["status"] == "resolved":
                raise ValueError("新问题不能直接标记为 resolved。")
            self._reject_reidentified_open_issue(issue)
            assigned = deepcopy(issue)
            assigned["issue_id"] = self._new_id(reserved)
            reserved.add(assigned["issue_id"])
            staged.append(assigned)
        return staged

    def _validate_known_issue(
        self,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> None:
        for field in _LOCKED_FIELDS:
            if current[field] != previous[field]:
                raise ValueError(f"已知问题的 {field} 不可修改。")

    def _validate_resolution(self, issue: dict[str, Any]) -> None:
        if not issue["source_refs"]:
            raise ValueError("resolved 问题必须包含非空 source_refs。")
        previous_explanations = {
            item["explanation"]
            for item in self._history
            if item["issue_id"] == issue["issue_id"]
        }
        if issue["explanation"] in previous_explanations:
            raise ValueError("resolved 问题必须提供区别于旧解释的闭合解释。")

    def _reject_reidentified_open_issue(self, issue: dict[str, Any]) -> None:
        identity = self._identity_key(issue)
        matches = [
            issue_id
            for issue_id, previous in self._current.items()
            if previous["status"] in _UNRESOLVED_STATUSES
            and self._identity_key(previous) == identity
        ]
        if matches:
            raise ValueError(
                "未关闭问题必须使用原稳定 issue_id，不能改 ID 绕过："
                + ", ".join(matches),
            )

    @staticmethod
    def _identity_key(issue: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(issue[field] for field in _IDENTITY_FIELDS)

    def _new_id(self, reserved: set[str]) -> str:
        self._allocated += 1
        issue_id = str(
            uuid5(self._namespace, f"review-issue:{self._allocated}")
            if self._namespace else uuid4()
        )
        if issue_id in self._current or issue_id in reserved:
            raise ValueError("程序生成的 issue_id 重复。")
        return issue_id
