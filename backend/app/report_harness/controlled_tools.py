from __future__ import annotations

import base64
import binascii
import json
import re
from copy import deepcopy
from typing import Any, Callable
from uuid import UUID

from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError


_MAX_IDS = 10
_MAX_OUTPUT_CHARS = 32000
_MAX_QUERY_CHARS = 1000
_MAX_TOP_K = 10
_MAX_KNOWLEDGE_ID_CHARS = 200
_CURSOR_VERSION = 1
_KNOWLEDGE_FIELDS = (
    "id",
    "document_id",
    "version",
    "text",
    "digest",
    "manifest_digest",
)
_EVIDENCE_STATUSES = frozenset({"unverified", "human_confirmed", "disputed"})
_EVIDENCE_KINDS = frozenset({"observation", "statement", "document_excerpt", "other"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ControlledTools:
    """为生成者和独立审查者提供只读、冻结版本绑定的上下文工具。"""

    def __init__(
        self,
        snapshot: dict,
        knowledge_chunks: list[dict],
        search: Callable[[str, int], list[dict]] | None = None,
    ) -> None:
        self._snapshot = self._validate_snapshot(snapshot)
        self._revision = self._snapshot["revision"]
        self._manifest_digest = self._snapshot["knowledge_manifest_digest"]
        try:
            self._snapshot_digest = canonical_digest(self._snapshot)
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "快照必须可编码为规范 JSON。"},
            ) from exc

        self._knowledge_chunks = self._validate_knowledge_chunks(
            knowledge_chunks, self._manifest_digest,
        )
        self._knowledge_by_id = {item["id"]: item for item in self._knowledge_chunks}
        if search is not None and not callable(search):
            raise HarnessError(
                "invalid_search_callback",
                422,
                {"reason": "search 必须是显式注入的可调用对象。"},
            )
        self._search = search
        self._search_registry: dict[str, dict] = {}
        self._issued_read_cursors: dict[str, dict] = {}
        self._accessed_evidence: dict[str, set[str]] = {
            "generator": set(),
            "reviewer": set(),
        }
        self._accessed_knowledge: dict[str, set[str]] = {
            "generator": set(),
            "reviewer": set(),
        }

        self._evidence_by_id, self._evidence_order = self._build_evidence_index()
        self._evidence_catalog = {
            evidence_id: self._catalog_item(item)
            for evidence_id, item in self._evidence_by_id.items()
        }

    def reset_access(self, role: str) -> None:
        """新候选轮次用新上下文复查，不沿用上一轮未传入的原文访问记录。"""
        self._validate_role(role)
        self._accessed_evidence[role].clear()
        self._accessed_knowledge[role].clear()
        self._issued_read_cursors = {
            cursor: binding for cursor, binding in self._issued_read_cursors.items()
            if binding["role"] != role
        }

    def checkpoint_state(self) -> dict:
        """仅供受控私有检查点使用，不接受来自模型或HTTP的状态。"""
        return {
            "snapshot_digest": self._snapshot_digest,
            "knowledge_digest": canonical_digest(self._knowledge_chunks),
            "registry": deepcopy(self._search_registry),
            "cursors": deepcopy(self._issued_read_cursors),
            "evidence": {role: sorted(ids) for role, ids in self._accessed_evidence.items()},
            "knowledge": {role: sorted(ids) for role, ids in self._accessed_knowledge.items()},
        }

    def restore_checkpoint_state(self, state: dict) -> None:
        try:
            self._restore_checkpoint_state(state)
        except HarnessError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise HarnessError("checkpoint_invalid") from exc

    def _restore_checkpoint_state(self, state: dict) -> None:
        if (state.get("snapshot_digest") != self._snapshot_digest
                or state.get("knowledge_digest") != canonical_digest(self._knowledge_chunks)):
            raise HarnessError("checkpoint_snapshot_mismatch")
        registry = deepcopy(state["registry"])
        for identifier, item in registry.items():
            approved = self._knowledge_by_id.get(identifier)
            if approved is None or item != approved:
                raise HarnessError("checkpoint_knowledge_mismatch")
        evidence, knowledge = {}, {}
        for role in ("generator", "reviewer"):
            evidence[role] = set(state["evidence"][role])
            knowledge[role] = set(state["knowledge"][role])
            if (not evidence[role] <= self._evidence_by_id.keys()
                    or not knowledge[role] <= self._knowledge_by_id.keys()):
                raise HarnessError("checkpoint_source_mismatch")
        cursors = deepcopy(state["cursors"])
        for binding in cursors.values():
            self._validate_role(binding["role"])
            if binding["kind"] not in {"evidence", "knowledge"}:
                raise HarnessError("checkpoint_cursor_mismatch")
            binding["request_ids"] = tuple(binding["request_ids"])
        self._search_registry = registry
        self._issued_read_cursors = cursors
        self._accessed_evidence, self._accessed_knowledge = evidence, knowledge

    def execute(self, role: str, name: str, args: dict) -> dict:
        """按固定工具名称执行一次受控读取或检索。"""
        self._validate_role(role)
        if not isinstance(name, str) or name not in {
            "list_evidence",
            "read_evidence",
            "search_knowledge",
            "read_knowledge",
        }:
            raise HarnessError(
                "unknown_tool",
                422,
                {"reason": "工具名称不在受控工具清单中。"},
            )
        if not isinstance(args, dict):
            raise HarnessError(
                "invalid_tool_arguments",
                422,
                {"tool": name, "reason": "工具参数必须是对象。"},
            )

        if name == "list_evidence":
            return self._list_evidence(args)
        if name == "read_evidence":
            return self._read_evidence(role, args)
        if name == "search_knowledge":
            return self._search_knowledge(role, args)
        return self._read_knowledge(role, args)

    def registered_knowledge(self) -> list[dict]:
        """返回当前快照批准的知识原文副本，不暴露内部可变对象。"""
        return deepcopy(self._knowledge_chunks)

    def accessed_evidence(self, role: str) -> set[str]:
        self._validate_role(role)
        return set(self._accessed_evidence[role])

    def accessed_knowledge(self, role: str) -> set[str]:
        self._validate_role(role)
        return set(self._accessed_knowledge[role])

    @staticmethod
    def _raise_invalid(reason: str, *, tool: str | None = None, **details: Any) -> None:
        payload = {"reason": reason, **details}
        if tool is not None:
            payload["tool"] = tool
        raise HarnessError("invalid_tool_arguments", 422, payload)

    @staticmethod
    def _validate_role(role: object) -> None:
        if not isinstance(role, str) or role not in {"generator", "reviewer"}:
            raise HarnessError(
                "invalid_tool_role",
                403,
                {"reason": "仅允许 generator 或 reviewer 角色。"},
            )

    @classmethod
    def _validate_snapshot(cls, snapshot: object) -> dict:
        if not isinstance(snapshot, dict):
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "snapshot 必须是对象。"},
            )
        required = {
            "accident_data",
            "supplemental_records",
            "revision",
            "knowledge_manifest_digest",
            "fact_obligations",
        }
        missing = sorted(required - set(snapshot))
        if missing:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "快照缺少必要字段。", "missing": missing},
            )
        if not isinstance(snapshot["accident_data"], dict):
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "accident_data 必须是对象。"},
            )
        revision = snapshot["revision"]
        if type(revision) is not int or revision < 0:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "revision 必须是非负整数。"},
            )
        manifest_digest = snapshot["knowledge_manifest_digest"]
        if not isinstance(manifest_digest, str) or not manifest_digest.strip():
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "knowledge_manifest_digest 必须是非空字符串。"},
            )
        if not isinstance(snapshot["supplemental_records"], list):
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "supplemental_records 必须是列表。"},
            )
        if len(snapshot["supplemental_records"]) > 50:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "补充证据不得超过 50 条。"},
            )
        if not isinstance(snapshot["fact_obligations"], list):
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "fact_obligations 必须是列表。"},
            )

        frozen = deepcopy(snapshot)
        try:
            json.dumps(frozen, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "快照必须只包含可序列化 JSON 值。"},
            ) from exc
        return frozen

    @classmethod
    def _validate_knowledge_chunks(
        cls, chunks: object, manifest_digest: str,
    ) -> list[dict]:
        if not isinstance(chunks, list):
            raise HarnessError(
                "knowledge_chunks_invalid",
                422,
                {"reason": "knowledge_chunks 必须是列表。"},
            )
        normalized: list[dict] = []
        seen: set[str] = set()
        # manifest digest 必须与当前冻结快照绑定，避免隐式读取外部清单。
        for index, raw in enumerate(chunks):
            if not isinstance(raw, dict):
                raise HarnessError(
                    "knowledge_chunks_invalid",
                    422,
                    {"reason": "知识片段必须是对象。", "index": index},
                )
            missing = [field for field in _KNOWLEDGE_FIELDS if field not in raw]
            if missing:
                raise HarnessError(
                    "knowledge_chunks_invalid",
                    422,
                    {"reason": "知识片段缺少必要字段。", "index": index, "missing": missing},
                )
            extra = sorted(set(raw) - set(_KNOWLEDGE_FIELDS))
            if extra:
                raise HarnessError(
                    "knowledge_chunks_invalid",
                    422,
                    {"reason": "知识片段包含未声明字段。", "index": index, "extra": extra},
                )
            item: dict[str, str] = {}
            for field in _KNOWLEDGE_FIELDS:
                value = raw[field]
                if not isinstance(value, str) or not value.strip():
                    raise HarnessError(
                        "knowledge_chunks_invalid",
                        422,
                        {
                            "reason": "知识片段字段必须是非空字符串。",
                            "index": index,
                            "field": field,
                        },
                    )
                item[field] = value
            for field in ("id", "document_id", "version"):
                if (
                    len(item[field]) > _MAX_KNOWLEDGE_ID_CHARS
                    or item[field] != item[field].strip()
                ):
                    raise HarnessError(
                        "knowledge_chunks_invalid",
                        422,
                        {
                            "reason": "知识片段 ID、document_id、version 必须是 1-200 字符且无首尾空白。",
                            "index": index,
                            "field": field,
                        },
                    )
            if _SHA256_RE.fullmatch(item["digest"]) is None:
                raise HarnessError(
                    "knowledge_chunks_invalid",
                    422,
                    {
                        "reason": "知识片段 digest 必须是 64 位小写 SHA-256。",
                        "index": index,
                        "field": "digest",
                    },
                )
            expected_digest = canonical_digest(item["text"])
            if item["digest"] != expected_digest:
                raise HarnessError(
                    "knowledge_chunks_invalid",
                    422,
                    {
                        "reason": "知识片段 digest 与原文 canonical_digest 不一致。",
                        "index": index,
                        "field": "digest",
                        "expected_digest": expected_digest,
                    },
                )
            if item["id"] in seen:
                raise HarnessError(
                    "knowledge_chunks_invalid",
                    422,
                    {"reason": "知识片段 ID 不得重复。", "chunk_id": item["id"]},
                )
            if item["manifest_digest"] != manifest_digest:
                raise HarnessError(
                    "knowledge_manifest_conflict",
                    409,
                    {
                        "reason": "知识片段不属于当前冻结 manifest。",
                        "chunk_id": item["id"],
                        "expected_manifest_digest": manifest_digest,
                        "actual_manifest_digest": item["manifest_digest"],
                    },
                )
            seen.add(item["id"])
            normalized.append(item)
        return normalized

    def _build_evidence_index(self) -> tuple[dict[str, dict], list[str]]:
        by_id: dict[str, dict] = {}
        for pointer, value in self._iter_accident_leaves(self._snapshot["accident_data"]):
            evidence_id = "accident:" + pointer
            by_id[evidence_id] = {
                "evidence_id": evidence_id,
                "text": self._accident_text(value),
                "source_label": "冻结事故输入",
                "source_locator": pointer,
                "kind": "observation",
                "verification_status": "unverified",
                "verification_note": None,
                "conflicts_with": [],
                "field_conflicts": [],
            }

        record_ids: set[str] = set()
        for index, record in enumerate(self._snapshot["supplemental_records"]):
            item = self._record_to_evidence(record, index)
            evidence_id = item["evidence_id"]
            if evidence_id in by_id or evidence_id in record_ids:
                raise HarnessError(
                    "snapshot_invalid",
                    422,
                    {"reason": "冻结证据 ID 不得重复。", "evidence_id": evidence_id},
                )
            record_ids.add(evidence_id)
            by_id[evidence_id] = item

        for item in self._snapshot["supplemental_records"]:
            conflicts = item.get("conflicts_with", [])
            if not isinstance(conflicts, list):
                raise HarnessError(
                    "snapshot_invalid",
                    422,
                    {"reason": "conflicts_with 必须是列表。"},
                )
            for conflict in conflicts:
                conflict_id = self._evidence_ref_from_uuid(conflict)
                if conflict_id not in record_ids:
                    raise HarnessError(
                        "snapshot_invalid",
                        422,
                        {
                            "reason": "conflicts_with 只能引用当前冻结记录。",
                            "evidence_id": self._evidence_ref_from_uuid(item.get("evidence_id")),
                            "conflict_id": conflict_id,
                        },
                    )

        obligation_order: list[str] = []
        seen_refs: set[str] = set()
        for index, obligation in enumerate(self._snapshot["fact_obligations"]):
            if not isinstance(obligation, dict):
                raise HarnessError(
                    "snapshot_invalid",
                    422,
                    {"reason": "事实义务必须是对象。", "index": index},
                )
            obligation_id = obligation.get("obligation_id")
            if not isinstance(obligation_id, str) or not obligation_id.strip():
                raise HarnessError(
                    "snapshot_invalid",
                    422,
                    {"reason": "事实义务缺少有效 obligation_id。", "index": index},
                )
            refs = obligation.get("source_refs")
            if not isinstance(refs, list) or not refs:
                raise HarnessError(
                    "snapshot_invalid",
                    422,
                    {"reason": "事实义务必须包含 source_refs。", "index": index},
                )
            local_refs: set[str] = set()
            for ref in refs:
                normalized_ref = self._validate_source_ref(ref)
                if normalized_ref in local_refs:
                    raise HarnessError(
                        "snapshot_invalid",
                        422,
                        {
                            "reason": "同一事实义务不能重复引用证据。",
                            "obligation_id": obligation_id,
                            "source_ref": normalized_ref,
                        },
                    )
                local_refs.add(normalized_ref)
                if normalized_ref not in seen_refs:
                    seen_refs.add(normalized_ref)
                    obligation_order.append(normalized_ref)

        if set(by_id) != set(obligation_order):
            raise HarnessError(
                "snapshot_source_refs_conflict",
                422,
                {
                    "reason": "证据索引必须与 fact_obligations.source_refs 完全一致。",
                    "missing_refs": sorted(set(by_id) - set(obligation_order)),
                    "unknown_refs": sorted(set(obligation_order) - set(by_id)),
                },
            )
        return by_id, obligation_order

    @classmethod
    def _record_to_evidence(cls, record: object, index: int) -> dict:
        if not isinstance(record, dict):
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "补充证据必须是对象。", "index": index},
            )
        required = {
            "evidence_id",
            "text",
            "source_label",
            "source_locator",
            "kind",
            "verification_status",
        }
        missing = sorted(required - set(record))
        if missing:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "补充证据缺少必要字段。", "index": index, "missing": missing},
            )
        evidence_ref = cls._evidence_ref_from_uuid(record["evidence_id"])
        text = record["text"]
        source_label = record["source_label"]
        source_locator = record["source_locator"]
        kind = record["kind"]
        verification_status = record["verification_status"]
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "补充证据 text 必须是 1-8000 字符。", "evidence_id": evidence_ref},
            )
        if not isinstance(source_label, str) or not source_label.strip() or len(source_label) > 200:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "补充证据 source_label 长度无效。", "evidence_id": evidence_ref},
            )
        if not isinstance(source_locator, str) or not source_locator.strip() or len(source_locator) > 500:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "补充证据 source_locator 长度无效。", "evidence_id": evidence_ref},
            )
        if not isinstance(kind, str) or kind not in _EVIDENCE_KINDS:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "补充证据 kind 无效。", "evidence_id": evidence_ref},
            )
        if (
            not isinstance(verification_status, str)
            or verification_status not in _EVIDENCE_STATUSES
        ):
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "补充证据 verification_status 无效。", "evidence_id": evidence_ref},
            )

        item = deepcopy(record)
        item["evidence_id"] = evidence_ref
        conflicts = item.get("conflicts_with", [])
        if not isinstance(conflicts, list):
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "conflicts_with 必须是列表。", "evidence_id": evidence_ref},
            )
        normalized_conflicts = [cls._evidence_ref_from_uuid(value) for value in conflicts]
        if len(set(normalized_conflicts)) != len(normalized_conflicts):
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "conflicts_with 不能重复。", "evidence_id": evidence_ref},
            )
        if evidence_ref in normalized_conflicts:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "conflicts_with 不能自指。", "evidence_id": evidence_ref},
            )
        item["conflicts_with"] = normalized_conflicts
        note = item.get("verification_note")
        if note is not None:
            if not isinstance(note, str) or len(note) > 1000:
                raise HarnessError(
                    "snapshot_invalid",
                    422,
                    {"reason": "verification_note 长度无效。", "evidence_id": evidence_ref},
                )
            if verification_status in {"human_confirmed", "disputed"} and not note.strip():
                raise HarnessError(
                    "snapshot_invalid",
                    422,
                    {"reason": "已核实或有争议的证据必须提供 verification_note。", "evidence_id": evidence_ref},
                )
        elif verification_status in {"human_confirmed", "disputed"}:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "已核实或有争议的证据必须提供 verification_note。", "evidence_id": evidence_ref},
            )
        item.setdefault("verification_note", None)
        item.setdefault("field_conflicts", [])
        return item

    @classmethod
    def _iter_accident_leaves(cls, value: Any, path: str = ""):
        if isinstance(value, dict) and value:
            for key, child in value.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                yield from cls._iter_accident_leaves(child, path + "/" + escaped)
            return
        if isinstance(value, list) and value:
            for index, child in enumerate(value):
                yield from cls._iter_accident_leaves(child, path + "/" + str(index))
            return
        yield path, value

    @staticmethod
    def _accident_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "事故字段必须可序列化为 JSON。"},
            ) from exc

    @staticmethod
    def _evidence_ref_from_uuid(value: object) -> str:
        try:
            normalized = str(UUID(str(value).strip()))
        except (AttributeError, ValueError, TypeError) as exc:
            raise HarnessError(
                "snapshot_invalid",
                422,
                {"reason": "证据 ID 必须是 UUID。"},
            ) from exc
        return "evidence:" + normalized

    @classmethod
    def _validate_source_ref(cls, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise HarnessError(
                "snapshot_source_refs_conflict",
                422,
                {"reason": "source_refs 只能包含非空字符串。"},
            )
        if value.startswith("evidence:"):
            normalized = cls._evidence_ref_from_uuid(value[len("evidence:"):])
            if normalized != value:
                raise HarnessError(
                    "snapshot_source_refs_conflict",
                    422,
                    {"reason": "evidence 引用必须使用规范 UUID。", "source_ref": value},
                )
            return normalized
        if value.startswith("accident:"):
            pointer = value[len("accident:"):]
            if pointer and not pointer.startswith("/"):
                raise HarnessError(
                    "snapshot_source_refs_conflict",
                    422,
                    {"reason": "accident 引用必须是 JSON Pointer。", "source_ref": value},
                )
            if re.search(r"~(?![01])", pointer):
                raise HarnessError(
                    "snapshot_source_refs_conflict",
                    422,
                    {"reason": "JSON Pointer 转义必须使用 ~0 或 ~1。", "source_ref": value},
                )
            return value
        raise HarnessError(
            "snapshot_source_refs_conflict",
            422,
            {"reason": "source_refs 只能引用 accident 或 evidence。", "source_ref": value},
        )

    @staticmethod
    def _catalog_item(item: dict) -> dict:
        return {
            "evidence_id": item["evidence_id"],
            "kind": item["kind"],
            "source_label": item["source_label"],
            "source_locator": item["source_locator"],
            "verification_status": item["verification_status"],
        }

    @classmethod
    def _validate_exact_keys(
        cls,
        args: dict,
        *,
        tool: str,
        required: set[str],
        optional: set[str] | None = None,
    ) -> None:
        allowed = required | (optional or set())
        keys = set(args)
        missing = sorted(required - keys)
        extra = sorted(keys - allowed)
        if missing or extra:
            cls._raise_invalid(
                "参数字段不符合严格 schema。",
                tool=tool,
                missing=missing,
                extra=extra,
            )

    @classmethod
    def _validate_ids(cls, args: dict, *, key: str, tool: str) -> list[str]:
        value = args.get(key)
        if not isinstance(value, list) or not 1 <= len(value) <= _MAX_IDS:
            cls._raise_invalid(
                f"{key} 必须是 1-{_MAX_IDS} 个 ID 的列表。",
                tool=tool,
                field=key,
            )
        if any(not isinstance(item, str) or not item.strip() for item in value):
            cls._raise_invalid(
                f"{key} 只能包含非空字符串。",
                tool=tool,
                field=key,
            )
        normalized = [item.strip() for item in value]
        if len(set(normalized)) != len(normalized):
            cls._raise_invalid(
                f"{key} 不能包含重复 ID。",
                tool=tool,
                field=key,
            )
        return normalized

    def _list_evidence(self, args: dict) -> dict:
        self._validate_exact_keys(args, tool="list_evidence", required=set(), optional={"cursor"})
        cursor = args.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            self._raise_invalid("cursor 必须是非空字符串。", tool="list_evidence", field="cursor")
        start = self._decode_cursor(cursor) if cursor is not None else 0

        items: list[dict] = []
        end = start
        while end < len(self._evidence_order):
            candidate = items + [self._evidence_catalog[self._evidence_order[end]]]
            has_more = end + 1 < len(self._evidence_order)
            next_cursor = self._encode_cursor(end + 1) if has_more else None
            payload = {
                "items": candidate,
                "next_cursor": next_cursor,
                "truncated": has_more,
            }
            if self._output_chars(payload) > _MAX_OUTPUT_CHARS:
                if not items:
                    self._raise_output_too_large(
                        "单个证据目录项无法放入 32000 字符响应。",
                        item_id=self._evidence_order[end],
                    )
                break
            items = candidate
            end += 1

        has_more = end < len(self._evidence_order)
        payload = {
            "items": items,
            "next_cursor": self._encode_cursor(end) if has_more else None,
            "truncated": has_more,
        }
        return self._checked_output(payload)

    def _read_evidence(self, role: str, args: dict) -> dict:
        tool = "read_evidence"
        self._validate_exact_keys(args, tool=tool, required={"evidence_ids"}, optional={"cursor"})
        evidence_ids = self._validate_ids(args, key="evidence_ids", tool=tool)
        items = []
        for evidence_id in evidence_ids:
            item = self._evidence_by_id.get(evidence_id)
            if item is None:
                raise HarnessError(
                    "evidence_not_found",
                    404,
                    {"reason": "证据不在当前冻结快照中。", "evidence_id": evidence_id},
                )
            items.append(deepcopy(item))
        cursor = self._read_cursor_argument(args, tool)
        return self._read_paginated(
            role=role,
            kind="evidence",
            request_ids=evidence_ids,
            items=items,
            cursor=cursor,
        )

    def _search_knowledge(self, role: str, args: dict) -> dict:
        tool = "search_knowledge"
        self._validate_exact_keys(args, tool=tool, required={"query", "top_k"})
        query = args["query"]
        top_k = args["top_k"]
        if not isinstance(query, str) or len(query) > _MAX_QUERY_CHARS or not query.strip():
            self._raise_invalid(
                f"query 必须是 1-{_MAX_QUERY_CHARS} 字符的非空字符串。",
                tool=tool,
                field="query",
            )
        if type(top_k) is not int or not 1 <= top_k <= _MAX_TOP_K:
            self._raise_invalid(
                f"top_k 必须是 1-{_MAX_TOP_K} 的整数。",
                tool=tool,
                field="top_k",
            )
        normalized_query = " ".join(query.split())

        if self._search is None:
            candidates = self._local_search(normalized_query, top_k)
            mode = "registered_local"
        else:
            try:
                candidates = self._search(normalized_query, top_k)
            except Exception as exc:  # noqa: BLE001
                raise HarnessError(
                    "knowledge_search_failed",
                    503,
                    {"reason": "显式知识检索回调失败。", "exception": type(exc).__name__},
                ) from exc
            mode = "injected_service"

        if not isinstance(candidates, list):
            raise HarnessError(
                "knowledge_search_invalid_response",
                502,
                {"reason": "知识检索回调必须返回列表。"},
            )
        if len(candidates) > top_k:
            raise HarnessError(
                "knowledge_search_invalid_response",
                502,
                {"reason": "知识检索回调返回结果超过 top_k。", "top_k": top_k},
            )
        validated = self._validate_search_candidates(candidates)
        for item in validated:
            self._search_registry[item["id"]] = deepcopy(item)
        full_payload = {"items": validated, "search_mode": mode}
        try:
            payload = self._checked_output(full_payload)
        except HarnessError as exc:
            if exc.code != "output_too_large" or len(validated) != 1:
                raise
            directory = {
                "id": validated[0]["id"],
                "document_id": validated[0]["document_id"],
                "version": validated[0]["version"],
                "digest": validated[0]["digest"],
                "manifest_digest": validated[0]["manifest_digest"],
                "text_length": len(validated[0]["text"]),
                "text_available": False,
            }
            payload = self._checked_output({
                "items": [directory],
                "search_mode": mode,
                "truncated": True,
            })
            return payload
        self._accessed_knowledge[role].update(item["id"] for item in validated)
        return payload

    def _read_knowledge(self, role: str, args: dict) -> dict:
        tool = "read_knowledge"
        self._validate_exact_keys(args, tool=tool, required={"chunk_ids"}, optional={"cursor"})
        chunk_ids = self._validate_ids(args, key="chunk_ids", tool=tool)
        items = []
        for chunk_id in chunk_ids:
            item = self._knowledge_by_id.get(chunk_id)
            if item is None:
                raise HarnessError(
                    "knowledge_not_authorized",
                    403,
                    {"reason": "知识片段不在当前批准集合中。", "chunk_id": chunk_id},
                )
            items.append(deepcopy(item))
        cursor = self._read_cursor_argument(args, tool)
        return self._read_paginated(
            role=role,
            kind="knowledge",
            request_ids=chunk_ids,
            items=items,
            cursor=cursor,
        )

    @classmethod
    def _read_cursor_argument(cls, args: dict, tool: str) -> str | None:
        cursor = args.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor):
            cls._raise_invalid("cursor 必须是非空字符串。", tool=tool, field="cursor")
        return cursor

    def _read_paginated(
        self,
        *,
        role: str,
        kind: str,
        request_ids: list[str],
        items: list[dict],
        cursor: str | None,
    ) -> dict:
        bindings = [self._content_binding(kind, item) for item in items]
        if cursor is None:
            position = (0, 0)
        else:
            position = self._decode_read_cursor(
                cursor,
                role=role,
                kind=kind,
                request_ids=request_ids,
                bindings=bindings,
                items=items,
            )

        payload, completed_ids = self._build_read_page(
            role=role,
            kind=kind,
            request_ids=request_ids,
            bindings=bindings,
            items=items,
            position=position,
        )
        result = self._checked_output(payload)
        if result["next_cursor"] is not None:
            self._issued_read_cursors[result["next_cursor"]] = {
                "role": role,
                "kind": kind,
                "request_ids": tuple(request_ids),
                "bindings": deepcopy(bindings),
            }
        accessed = self._accessed_evidence if kind == "evidence" else self._accessed_knowledge
        accessed[role].update(completed_ids)
        return result

    def _build_read_page(
        self,
        *,
        role: str,
        kind: str,
        request_ids: list[str],
        bindings: list[dict],
        items: list[dict],
        position: tuple[int, int],
    ) -> tuple[dict, set[str]]:
        page_items: list[dict] = []
        completed_ids: set[str] = set()
        index, offset = position

        while index < len(items):
            item = items[index]
            text = item["text"]
            full_item = self._text_segment(item, offset, len(text))
            next_position = (index + 1, 0)
            if self._read_payload_fits(
                page_items + [full_item],
                role=role,
                kind=kind,
                request_ids=request_ids,
                bindings=bindings,
                next_position=next_position if next_position[0] < len(items) else None,
            ):
                page_items.append(full_item)
                completed_ids.add(request_ids[index])
                index, offset = next_position
                continue

            remaining = len(text) - offset
            fitting = self._largest_fitting_segment(
                page_items=page_items,
                item=item,
                index=index,
                offset=offset,
                remaining=remaining,
                role=role,
                kind=kind,
                request_ids=request_ids,
                bindings=bindings,
                total_items=len(items),
            )
            if fitting <= 0:
                if not page_items:
                    self._raise_output_too_large(
                        "单个原文片段无法放入 32000 字符响应。",
                        item_id=request_ids[index],
                    )
                break

            segment_end = offset + fitting
            page_items.append(self._text_segment(item, offset, segment_end))
            if segment_end == len(text):
                completed_ids.add(request_ids[index])
                index, offset = index + 1, 0
            else:
                index, offset = index, segment_end
            break

        next_position = (index, offset) if index < len(items) else None
        return (
            self._read_payload(
                page_items,
                role=role,
                kind=kind,
                request_ids=request_ids,
                bindings=bindings,
                next_position=next_position,
            ),
            completed_ids,
        )

    def _largest_fitting_segment(
        self,
        *,
        page_items: list[dict],
        item: dict,
        index: int,
        offset: int,
        remaining: int,
        role: str,
        kind: str,
        request_ids: list[str],
        bindings: list[dict],
        total_items: int,
    ) -> int:
        low, high, best = 1, remaining, 0
        while low <= high:
            middle = (low + high) // 2
            end = offset + middle
            next_position = (index + 1, 0) if end == len(item["text"]) else (index, end)
            candidate = self._text_segment(item, offset, end)
            if self._read_payload_fits(
                page_items + [candidate],
                role=role,
                kind=kind,
                request_ids=request_ids,
                bindings=bindings,
                next_position=next_position if next_position[0] < total_items else None,
            ):
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        return best

    @staticmethod
    def _text_segment(item: dict, start: int, end: int) -> dict:
        segment = deepcopy(item)
        segment["text"] = item["text"][start:end]
        segment["text_start"] = start
        segment["text_end"] = end
        segment["text_length"] = len(item["text"])
        segment["text_complete"] = end == len(item["text"])
        return segment

    def _read_payload_fits(
        self,
        items: list[dict],
        *,
        role: str,
        kind: str,
        request_ids: list[str],
        bindings: list[dict],
        next_position: tuple[int, int] | None,
    ) -> bool:
        payload = self._read_payload(
            items,
            role=role,
            kind=kind,
            request_ids=request_ids,
            bindings=bindings,
            next_position=next_position,
        )
        return self._output_chars(payload) <= _MAX_OUTPUT_CHARS

    def _read_payload(
        self,
        items: list[dict],
        *,
        role: str,
        kind: str,
        request_ids: list[str],
        bindings: list[dict],
        next_position: tuple[int, int] | None,
    ) -> dict:
        return {
            "items": items,
            "next_cursor": (
                self._encode_read_cursor(
                    role=role,
                    kind=kind,
                    request_ids=request_ids,
                    bindings=bindings,
                    position=next_position,
                )
                if next_position is not None
                else None
            ),
            "truncated": next_position is not None,
        }

    def _content_binding(self, kind: str, item: dict) -> dict:
        if kind == "knowledge":
            return {"version": item["version"], "digest": item["digest"]}
        return {"version": str(self._revision), "digest": canonical_digest(item)}

    def _decode_read_cursor(
        self,
        cursor: str,
        *,
        role: str,
        kind: str,
        request_ids: list[str],
        bindings: list[dict],
        items: list[dict],
    ) -> tuple[int, int]:
        issued = self._issued_read_cursors.get(cursor)
        if issued is None:
            raise HarnessError(
                "cursor_invalid",
                409,
                {"reason": "cursor 未由当前读取请求实际发出。"},
            )
        try:
            padding = "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, binascii.Error, json.JSONDecodeError, ValueError) as exc:
            raise HarnessError(
                "cursor_invalid",
                409,
                {"reason": "cursor 不是当前读取请求生成的有效游标。"},
            ) from exc
        required = {
            "v",
            "kind",
            "role",
            "request_ids",
            "snapshot_digest",
            "revision",
            "content_bindings",
            "item_index",
            "text_offset",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise HarnessError(
                "cursor_invalid",
                409,
                {"reason": "读取 cursor schema 无效。"},
            )
        position = (payload["item_index"], payload["text_offset"])
        if (
            payload["v"] != _CURSOR_VERSION
            or payload["kind"] != kind
            or payload["role"] != role
            or payload["request_ids"] != request_ids
            or payload["snapshot_digest"] != self._snapshot_digest
            or payload["revision"] != self._revision
            or payload["content_bindings"] != bindings
            or issued["role"] != role
            or issued["kind"] != kind
            or issued["request_ids"] != tuple(request_ids)
            or issued["bindings"] != bindings
            or type(position[0]) is not int
            or type(position[1]) is not int
            or not 0 <= position[0] < len(items)
            or not 0 <= position[1] < len(items[position[0]]["text"])
        ):
            raise HarnessError(
                "cursor_invalid",
                409,
                {"reason": "cursor 不属于当前角色、请求或冻结内容版本。"},
            )
        return position

    def _encode_read_cursor(
        self,
        *,
        role: str,
        kind: str,
        request_ids: list[str],
        bindings: list[dict],
        position: tuple[int, int],
    ) -> str:
        payload = {
            "v": _CURSOR_VERSION,
            "kind": kind,
            "role": role,
            "request_ids": request_ids,
            "snapshot_digest": self._snapshot_digest,
            "revision": self._revision,
            "content_bindings": bindings,
            "item_index": position[0],
            "text_offset": position[1],
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def _local_search(self, query: str, top_k: int) -> list[dict]:
        folded_query = query.casefold()
        tokens = [token for token in re.split(r"\s+", folded_query) if token]
        matches: list[dict] = []
        for item in self._knowledge_chunks:
            haystack = "\n".join(
                (item["text"], item["document_id"], item["version"]),
            ).casefold()
            if folded_query in haystack or all(token in haystack for token in tokens):
                matches.append(deepcopy(item))
            if len(matches) >= top_k:
                break
        return matches

    def _validate_search_candidates(self, candidates: list[dict]) -> list[dict]:
        seen: set[str] = set()
        validated: list[dict] = []
        for index, candidate in enumerate(candidates):
            if not isinstance(candidate, dict):
                raise HarnessError(
                    "knowledge_search_invalid_response",
                    502,
                    {"reason": "检索结果必须是对象。", "index": index},
                )
            missing = [field for field in _KNOWLEDGE_FIELDS if field not in candidate]
            extra = sorted(set(candidate) - set(_KNOWLEDGE_FIELDS))
            if missing or extra:
                raise HarnessError(
                    "knowledge_search_invalid_response",
                    502,
                    {
                        "reason": "检索结果不符合知识片段 schema。",
                        "index": index,
                        "missing": missing,
                        "extra": extra,
                    },
                )
            chunk_id = candidate["id"]
            if not isinstance(chunk_id, str) or not chunk_id:
                raise HarnessError(
                    "knowledge_search_invalid_response",
                    502,
                    {"reason": "检索结果缺少有效 ID。", "index": index},
                )
            for field in ("document_id", "version", "text", "digest", "manifest_digest"):
                value = candidate[field]
                if not isinstance(value, str) or not value.strip():
                    raise HarnessError(
                        "knowledge_search_invalid_response",
                        502,
                        {
                            "reason": "检索结果字段必须是非空字符串。",
                            "index": index,
                            "field": field,
                        },
                    )
            if (
                len(candidate["document_id"]) > _MAX_KNOWLEDGE_ID_CHARS
                or len(candidate["version"]) > _MAX_KNOWLEDGE_ID_CHARS
                or candidate["document_id"] != candidate["document_id"].strip()
                or candidate["version"] != candidate["version"].strip()
            ):
                raise HarnessError(
                    "knowledge_search_invalid_response",
                    502,
                    {"reason": "检索结果 document_id 或 version 约束无效。", "index": index},
                )
            if _SHA256_RE.fullmatch(candidate["digest"]) is None:
                raise HarnessError(
                    "knowledge_digest_conflict",
                    409,
                    {"reason": "检索结果 digest 格式无效。", "chunk_id": chunk_id},
                )
            if candidate["digest"] != canonical_digest(candidate["text"]):
                raise HarnessError(
                    "knowledge_digest_conflict",
                    409,
                    {"reason": "检索结果 digest 与原文不一致。", "chunk_id": chunk_id},
                )
            if chunk_id in seen:
                raise HarnessError(
                    "knowledge_search_invalid_response",
                    502,
                    {"reason": "检索结果不能包含重复 ID。", "chunk_id": chunk_id},
                )
            seen.add(chunk_id)
            approved = self._knowledge_by_id.get(chunk_id)
            if approved is None:
                raise HarnessError(
                    "knowledge_not_authorized",
                    403,
                    {"reason": "检索结果不在当前批准集合中。", "chunk_id": chunk_id},
                )
            for field in _KNOWLEDGE_FIELDS:
                if candidate[field] != approved[field]:
                    if field == "manifest_digest":
                        code = "knowledge_manifest_conflict"
                    elif field == "version":
                        code = "knowledge_version_conflict"
                    elif field == "digest":
                        code = "knowledge_digest_conflict"
                    else:
                        code = "knowledge_snapshot_conflict"
                    raise HarnessError(
                        code,
                        409,
                        {
                            "reason": "检索结果与冻结知识片段不一致。",
                            "chunk_id": chunk_id,
                            "field": field,
                        },
                    )
            validated.append(deepcopy(approved))
        return validated

    def _decode_cursor(self, cursor: str) -> int:
        try:
            padding = "=" * (-len(cursor) % 4)
            raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, binascii.Error, json.JSONDecodeError, ValueError) as exc:
            raise HarnessError(
                "cursor_invalid",
                409,
                {"reason": "cursor 不是当前工具生成的有效游标。"},
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {"v", "digest", "revision", "offset"}:
            raise HarnessError(
                "cursor_invalid",
                409,
                {"reason": "cursor schema 无效。"},
            )
        if (
            payload["v"] != _CURSOR_VERSION
            or payload["digest"] != self._snapshot_digest
            or payload["revision"] != self._revision
            or type(payload["offset"]) is not int
            or not 1 <= payload["offset"] <= len(self._evidence_order)
        ):
            raise HarnessError(
                "cursor_invalid",
                409,
                {"reason": "cursor 不属于当前冻结快照版本。"},
            )
        return payload["offset"]

    def _encode_cursor(self, offset: int) -> str:
        payload = {
            "v": _CURSOR_VERSION,
            "digest": self._snapshot_digest,
            "revision": self._revision,
            "offset": offset,
        }
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _output_chars(payload: dict) -> int:
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, UnicodeError) as exc:
            raise HarnessError(
                "tool_output_invalid",
                500,
                {"reason": "工具输出不能编码为 JSON。"},
            ) from exc
        return len(encoded)

    @classmethod
    def _checked_output(cls, payload: dict) -> dict:
        chars = cls._output_chars(payload)
        if chars > _MAX_OUTPUT_CHARS:
            cls._raise_output_too_large(
                "工具输出超过 32000 字符，读取工具不会静默截断。",
                actual_chars=chars,
            )
        return deepcopy(payload)

    @staticmethod
    def _raise_output_too_large(reason: str, **details: Any) -> None:
        raise HarnessError(
            "output_too_large",
            413,
            {"reason": reason, "max_chars": _MAX_OUTPUT_CHARS, **details},
        )
