from __future__ import annotations

import base64
import json
from copy import deepcopy
from uuid import uuid4

import pytest

from app.report_harness.contracts import canonical_digest
from app.report_harness.controlled_tools import ControlledTools
from app.report_harness.errors import HarnessError
from app.report_harness.evidence import audit_records, freeze_snapshot
from app.schemas.report_run import EvidenceRecord


def evidence(**changes):
    return {
        "evidence_id": str(uuid4()),
        "text": "合成补充记录，尚待核实。",
        "source_label": "合成记录",
        "source_locator": "第 1 页",
        "kind": "statement",
        "verification_status": "unverified",
        **changes,
    }


def chunk(*, chunk_id="chunk-1", text="路口安全规则原文"):
    return {
        "id": chunk_id,
        "document_id": "doc-1",
        "version": "v1",
        "text": text,
        "digest": canonical_digest(text),
        "manifest_digest": "manifest-v1",
    }


def make_tools(*, accident=None, records=None, chunks=None, search=None):
    accident = accident or {"事故经过": "车辆在路口发生碰撞", "地点/编号": ["A", "B"]}
    records = records or []
    snapshot = freeze_snapshot(
        accident,
        audit_records([EvidenceRecord(**item) for item in records], "owner"),
        3,
        "manifest-v1",
    )
    return ControlledTools(snapshot, chunks or [chunk()], search=search), snapshot


def assert_output_size(payload):
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) <= 32000


def test_new_candidate_round_resets_only_its_roles_access_and_cursors():
    tools, _ = make_tools(chunks=[chunk(text="合成原文" * 12000)])
    args = {"chunk_ids": ["chunk-1"]}
    generator = tools.execute("generator", "read_knowledge", args)
    reviewer = tools.execute("reviewer", "read_knowledge", args)
    assert generator["next_cursor"] and reviewer["next_cursor"]
    tools.execute("generator", "read_evidence", {"evidence_ids": ["accident:/事故经过"]})
    assert tools.accessed_evidence("generator")
    tools.reset_access("generator")
    assert not tools.accessed_evidence("generator")
    with pytest.raises(HarnessError):
        tools.execute("generator", "read_knowledge", {**args, "cursor": generator["next_cursor"]})
    tools.execute("reviewer", "read_knowledge", {**args, "cursor": reviewer["next_cursor"]})


def test_evidence_ids_and_provenance_are_frozen_and_readable():
    record = evidence(verification_status="human_confirmed", verification_note="人工核对原始记录")
    tools, snapshot = make_tools(records=[record])
    evidence_id = "evidence:" + record["evidence_id"]

    listed = tools.execute("generator", "list_evidence", {})
    ids = [item["evidence_id"] for item in listed["items"]]
    assert "accident:/地点~1编号/0" in ids
    assert evidence_id in ids

    read = tools.execute("reviewer", "read_evidence", {"evidence_ids": [evidence_id]})
    item = read["items"][0]
    assert item["text"] == record["text"]
    assert item["source_label"] == record["source_label"]
    assert item["source_locator"] == record["source_locator"]
    assert item["verification_status"] == "human_confirmed"
    assert snapshot["fact_obligations"]

    item["text"] = "不能污染冻结快照"
    assert tools.execute("generator", "read_evidence", {"evidence_ids": [evidence_id]})["items"][0]["text"] == record["text"]


def test_bare_evidence_uuid_is_normalized_and_access_is_role_scoped():
    record = evidence()
    tools, _ = make_tools(records=[record])
    bare_id = record["evidence_id"]
    canonical_id = "evidence:" + bare_id

    result = tools.execute("reviewer", "read_evidence", {"evidence_ids": [bare_id]})

    assert result["items"][0]["evidence_id"] == canonical_id
    assert tools.accessed_evidence("reviewer") == {canonical_id}
    assert tools.accessed_evidence("generator") == set()

    with pytest.raises(HarnessError) as error:
        tools.execute(
            "reviewer",
            "read_evidence",
            {"evidence_ids": [bare_id, canonical_id]},
        )
    assert error.value.code == "invalid_tool_arguments"


def test_read_evidence_rejects_malformed_unknown_and_cross_snapshot_ids():
    record = evidence()
    tools, _ = make_tools(records=[record])
    canonical_id = "evidence:" + record["evidence_id"]

    with pytest.raises(HarnessError) as error:
        tools.execute("reviewer", "read_evidence", {"evidence_ids": ["not-a-uuid"]})
    assert error.value.code == "invalid_tool_arguments"

    with pytest.raises(HarnessError) as error:
        tools.execute("reviewer", "read_evidence", {"evidence_ids": [str(uuid4())]})
    assert error.value.code == "evidence_not_found"

    other, _ = make_tools(records=[evidence()])
    with pytest.raises(HarnessError) as error:
        other.execute("reviewer", "read_evidence", {"evidence_ids": [canonical_id]})
    assert error.value.code == "evidence_not_found"


def test_bare_and_canonical_evidence_ids_share_cursor_binding():
    records = [
        evidence(text="X" * 8000, source_locator=f"位置-{index}")
        for index in range(5)
    ]
    tools, _ = make_tools(records=records)
    bare_ids = [record["evidence_id"] for record in records]
    canonical_ids = ["evidence:" + value for value in bare_ids]

    page = tools.execute("reviewer", "read_evidence", {"evidence_ids": bare_ids})
    assert page["next_cursor"] is not None
    assert all(item["evidence_id"] in canonical_ids for item in page["items"])

    cursor = page["next_cursor"]
    while cursor is not None:
        page = tools.execute(
            "reviewer",
            "read_evidence",
            {"evidence_ids": canonical_ids, "cursor": cursor},
        )
        cursor = page["next_cursor"]

    assert tools.accessed_evidence("reviewer") == set(canonical_ids)


def test_accessed_ids_are_role_scoped_and_list_is_not_a_read():
    record = evidence()
    tools, _ = make_tools(records=[record])
    evidence_id = "evidence:" + record["evidence_id"]

    tools.execute("generator", "list_evidence", {})
    assert tools.accessed_evidence("generator") == set()
    tools.execute("generator", "read_evidence", {"evidence_ids": [evidence_id]})
    assert tools.accessed_evidence("generator") == {evidence_id}
    assert tools.accessed_evidence("reviewer") == set()

    exposed = tools.accessed_evidence("generator")
    exposed.add("evidence:" + str(uuid4()))
    assert tools.accessed_evidence("generator") == {evidence_id}

    tools.execute("reviewer", "read_knowledge", {"chunk_ids": ["chunk-1"]})
    assert tools.accessed_knowledge("reviewer") == {"chunk-1"}
    assert tools.accessed_knowledge("generator") == set()

    tools.execute("generator", "search_knowledge", {"query": "路口", "top_k": 1})
    assert tools.accessed_knowledge("generator") == {"chunk-1"}

    with pytest.raises(HarnessError):
        tools.accessed_evidence("admin")


def test_strict_role_tool_and_argument_schema():
    tools, _ = make_tools()
    with pytest.raises(HarnessError) as error:
        tools.execute("admin", "list_evidence", {})
    assert error.value.code == "invalid_tool_role"

    with pytest.raises(HarnessError) as error:
        tools.execute("generator", "shell", {"command": "dir"})
    assert error.value.code == "unknown_tool"

    with pytest.raises(HarnessError) as error:
        tools.execute("generator", "read_evidence", {"evidence_ids": [], "extra": True})
    assert error.value.code == "invalid_tool_arguments"

    with pytest.raises(HarnessError):
        tools.execute("generator", "read_evidence", {"evidence_ids": ["x"] * 11})
    with pytest.raises(HarnessError):
        tools.execute("generator", "search_knowledge", {"query": "x", "top_k": 11})
    with pytest.raises(HarnessError):
        tools.execute("generator", "search_knowledge", {"query": "x" * 1001, "top_k": 1})


def test_list_evidence_paginates_without_dropping_items():
    records = [
        evidence(
            source_label="L" * 200,
            source_locator=f"位置-{index}-" + "P" * 490,
        )
        for index in range(50)
    ]
    tools, _ = make_tools(records=records)
    cursor = None
    collected = []
    pages = 0
    while True:
        args = {} if cursor is None else {"cursor": cursor}
        page = tools.execute("generator", "list_evidence", args)
        assert_output_size(page)
        collected.extend(item["evidence_id"] for item in page["items"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            assert page["truncated"] is False
            break
        assert page["truncated"] is True
    assert pages > 1
    assert len(collected) == len(set(collected)) == 53

    with pytest.raises(HarnessError) as error:
        tools.execute("generator", "list_evidence", {"cursor": "bad-cursor"})
    assert error.value.code == "cursor_invalid"


def test_list_cursor_is_bound_to_the_frozen_snapshot():
    def wide_records():
        return [
            evidence(
                source_label="L" * 200,
                source_locator=f"位置-{index}-" + "P" * 490,
            )
            for index in range(50)
        ]

    first, _ = make_tools(accident={"事实": "一"}, records=wide_records())
    second, _ = make_tools(accident={"事实": "二"}, records=wide_records())
    cursor = first.execute("generator", "list_evidence", {})["next_cursor"]
    assert cursor is not None
    with pytest.raises(HarnessError) as error:
        second.execute("generator", "list_evidence", {"cursor": cursor})
    assert error.value.code == "cursor_invalid"


def test_read_evidence_rejects_oversize_instead_of_truncating():
    tools, _ = make_tools(accident={"超长事实": "X" * 33000})
    evidence_id = "accident:/超长事实"
    page = tools.execute("generator", "read_evidence", {"evidence_ids": [evidence_id]})
    assert page["truncated"] is True
    assert page["items"][0]["text_complete"] is False
    assert page["items"][0]["text_start"] == 0
    assert tools.accessed_evidence("generator") == set()

    parts = [page["items"][0]["text"]]
    cursor = page["next_cursor"]
    while cursor is not None:
        page = tools.execute(
            "generator",
            "read_evidence",
            {"evidence_ids": [evidence_id], "cursor": cursor},
        )
        assert_output_size(page)
        parts.append(page["items"][0]["text"])
        cursor = page["next_cursor"]
    assert "".join(parts) == "X" * 33000
    assert tools.accessed_evidence("generator") == {evidence_id}


def test_registered_knowledge_is_a_deep_copy_and_direct_read_is_authorized():
    tools, _ = make_tools(chunks=[chunk(), chunk(chunk_id="chunk-2", text="责任认定原文")])
    registered = tools.registered_knowledge()
    registered[0]["text"] = "外部修改"
    assert tools.registered_knowledge()[0]["text"] == "路口安全规则原文"

    read = tools.execute("reviewer", "read_knowledge", {"chunk_ids": ["chunk-2"]})
    assert read["items"][0]["text"] == "责任认定原文"
    with pytest.raises(HarnessError) as error:
        tools.execute("reviewer", "read_knowledge", {"chunk_ids": ["unknown"]})
    assert error.value.code == "knowledge_not_authorized"


def test_knowledge_source_kind_is_optional_and_strict():
    legacy = chunk()
    tools, _ = make_tools(chunks=[legacy])
    assert "source_kind" not in tools.registered_knowledge()[0]

    for source_kind in ("rule_excerpt", "source_chunk"):
        approved = chunk(chunk_id=f"{source_kind}-1")
        approved["source_kind"] = source_kind
        tools, _ = make_tools(
            chunks=[approved],
            search=lambda _query, _top_k, item=approved: [deepcopy(item)],
        )
        assert tools.registered_knowledge()[0]["source_kind"] == source_kind
        assert tools.execute(
            "reviewer", "read_knowledge", {"chunk_ids": [approved["id"]]}
        )["items"][0]["source_kind"] == source_kind
        assert tools.execute(
            "reviewer", "search_knowledge", {"query": "规则", "top_k": 1}
        )["items"][0]["source_kind"] == source_kind

    invalid = chunk()
    invalid["source_kind"] = "other"
    with pytest.raises(HarnessError) as error:
        ControlledTools(make_tools()[1], [invalid])
    assert error.value.code == "knowledge_chunks_invalid"

    extra = chunk()
    extra["unexpected"] = "拒绝"
    with pytest.raises(HarnessError) as error:
        ControlledTools(make_tools()[1], [extra])
    assert error.value.code == "knowledge_chunks_invalid"


def test_without_callback_search_is_explicitly_limited_to_registered_chunks():
    tools, _ = make_tools(chunks=[chunk(), chunk(chunk_id="chunk-2", text="责任认定原文")])
    result = tools.execute(
        "generator",
        "search_knowledge",
        {"query": "责任认定", "top_k": 10},
    )
    assert result["search_mode"] == "registered_local"
    assert [item["id"] for item in result["items"]] == ["chunk-2"]
    assert "score" not in result["items"][0]
    assert tools.execute(
        "generator",
        "search_knowledge",
        {"query": "不存在的片段", "top_k": 10},
    )["items"] == []


def test_injected_search_must_return_the_approved_manifest_and_version():
    approved = chunk()
    calls = []

    def search(query, top_k):
        calls.append((query, top_k))
        return [deepcopy(approved)]

    tools, _ = make_tools(chunks=[approved], search=search)
    result = tools.execute("reviewer", "search_knowledge", {"query": "规则", "top_k": 2})
    assert calls == [("规则", 2)]
    assert result["search_mode"] == "injected_service"
    assert result["items"] == [approved]

    def bad_manifest(_query, _top_k):
        item = deepcopy(approved)
        item["manifest_digest"] = "other-manifest"
        return [item]

    tools, _ = make_tools(chunks=[approved], search=bad_manifest)
    with pytest.raises(HarnessError) as error:
        tools.execute("generator", "search_knowledge", {"query": "规则", "top_k": 1})
    assert error.value.code == "knowledge_manifest_conflict"

    def bad_version(_query, _top_k):
        item = deepcopy(approved)
        item["version"] = "v2"
        return [item]

    tools, _ = make_tools(chunks=[approved], search=bad_version)
    with pytest.raises(HarnessError) as error:
        tools.execute("generator", "search_knowledge", {"query": "规则", "top_k": 1})
    assert error.value.code == "knowledge_version_conflict"

    def bad_digest(_query, _top_k):
        item = deepcopy(approved)
        item["digest"] = "changed-digest"
        return [item]

    tools, _ = make_tools(chunks=[approved], search=bad_digest)
    with pytest.raises(HarnessError) as error:
        tools.execute("generator", "search_knowledge", {"query": "规则", "top_k": 1})
    assert error.value.code == "knowledge_digest_conflict"


def test_snapshot_source_refs_and_knowledge_schema_are_validated():
    _, snapshot = make_tools()
    invalid = deepcopy(snapshot)
    invalid["fact_obligations"][0]["source_refs"] = ["evidence:" + str(uuid4())]
    with pytest.raises(HarnessError) as error:
        ControlledTools(invalid, [chunk()])
    assert error.value.code == "snapshot_source_refs_conflict"

    invalid_chunk = chunk()
    invalid_chunk["manifest_digest"] = "wrong"
    with pytest.raises(HarnessError) as error:
        ControlledTools(snapshot, [invalid_chunk])
    assert error.value.code == "knowledge_manifest_conflict"

    with pytest.raises(HarnessError) as error:
        ControlledTools(snapshot, [{"id": "x"}])
    assert error.value.code == "knowledge_chunks_invalid"


def test_read_knowledge_rejects_oversize_without_silent_truncation():
    large = chunk(text="K" * 33000)
    tools, _ = make_tools(chunks=[large])
    first = tools.execute("reviewer", "read_knowledge", {"chunk_ids": [large["id"]]})
    assert first["truncated"] is True
    assert first["items"][0]["text_complete"] is False
    assert tools.accessed_knowledge("reviewer") == set()

    parts = [first["items"][0]["text"]]
    cursor = first["next_cursor"]
    while cursor is not None:
        page = tools.execute(
            "reviewer",
            "read_knowledge",
            {"chunk_ids": [large["id"]], "cursor": cursor},
        )
        assert_output_size(page)
        parts.append(page["items"][0]["text"])
        cursor = page["next_cursor"]
    assert "".join(parts) == large["text"]
    assert tools.accessed_knowledge("reviewer") == {large["id"]}


def test_partial_knowledge_checkpoint_cannot_claim_complete_read():
    large = chunk(text="K" * 33000)
    tools, _ = make_tools(chunks=[large])
    first = tools.execute("reviewer", "read_knowledge", {"chunk_ids": [large["id"]]})
    assert first["next_cursor"] is not None
    state = tools.checkpoint_state()
    assert state["knowledge"]["reviewer"] == []
    first_range = state["knowledge_read_ranges"]["reviewer"][large["id"]]
    assert first_range[0][0] == 0
    assert 0 < first_range[0][1] < len(large["text"])

    tampered = deepcopy(state)
    tampered["knowledge"]["reviewer"] = [large["id"]]
    restored, _ = make_tools(chunks=[large])
    with pytest.raises(HarnessError) as error:
        restored.restore_checkpoint_state(tampered)
    assert error.value.code == "checkpoint_source_mismatch"


def test_disconnected_knowledge_ranges_do_not_cover_missing_middle():
    large = chunk(text="K" * 33000)
    tools, _ = make_tools(chunks=[large])
    state = tools.checkpoint_state()
    state["knowledge_read_ranges"]["reviewer"] = {
        large["id"]: [[0, 100], [200, len(large["text"])]],
    }
    state["knowledge"]["reviewer"] = [large["id"]]
    with pytest.raises(HarnessError, match="checkpoint_source_mismatch"):
        tools.restore_checkpoint_state(state)
    state["knowledge"]["reviewer"] = []
    tools.restore_checkpoint_state(state)
    assert tools.accessed_knowledge("reviewer") == set()
    tools._record_knowledge_range("reviewer", large["id"], 100, 200)
    assert tools.accessed_knowledge("reviewer") == {large["id"]}


def test_single_oversize_search_result_returns_directory_without_access():
    large = chunk(text="R" * 33000)
    tools, _ = make_tools(chunks=[large])
    result = tools.execute(
        "generator",
        "search_knowledge",
        {"query": "RRRR", "top_k": 1},
    )
    assert result["truncated"] is True
    assert result["items"] == [{
        "id": large["id"],
        "document_id": large["document_id"],
        "version": large["version"],
        "digest": large["digest"],
        "manifest_digest": large["manifest_digest"],
        "text_length": len(large["text"]),
        "text_available": False,
    }]
    assert tools.accessed_knowledge("generator") == set()


def test_read_cursor_is_bound_to_role_and_requested_ids():
    tools, _ = make_tools(
        chunks=[chunk(text="A" * 33000), chunk(chunk_id="chunk-2", text="小片段")],
    )
    first = tools.execute(
        "generator",
        "read_knowledge",
        {"chunk_ids": ["chunk-1"]},
    )
    assert first["next_cursor"]
    cursor = first["next_cursor"]
    with pytest.raises(HarnessError) as error:
        tools.execute(
            "reviewer",
            "read_knowledge",
            {"chunk_ids": ["chunk-1"], "cursor": cursor},
        )
    assert error.value.code == "cursor_invalid"
    with pytest.raises(HarnessError) as error:
        tools.execute(
            "generator",
            "read_knowledge",
            {"chunk_ids": ["chunk-2"], "cursor": cursor},
        )
    assert error.value.code == "cursor_invalid"

    padding = "=" * (-len(cursor) % 4)
    payload = json.loads(base64.urlsafe_b64decode((cursor + padding).encode("ascii")))
    payload["text_offset"] = len("A" * 33000) - 1
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    tampered = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    with pytest.raises(HarnessError) as error:
        tools.execute(
            "generator",
            "read_knowledge",
            {"chunk_ids": ["chunk-1"], "cursor": tampered},
        )
    assert error.value.code == "cursor_invalid"


def test_chunk_digest_is_canonical_sha256_of_original_text():
    tools, _ = make_tools()
    assert tools.registered_knowledge()[0]["digest"] == canonical_digest("路口安全规则原文")

    invalid = chunk()
    invalid["digest"] = "0" * 64
    with pytest.raises(HarnessError) as error:
        ControlledTools(make_tools()[1], [invalid])
    assert error.value.code == "knowledge_chunks_invalid"
    assert error.value.details["field"] == "digest"
