from copy import deepcopy
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pytest
from pypdf import PdfReader

from app.report_harness.errors import HarnessError
from app.report_harness.exports import (
    ENGINEERING_MARKER, UNREVIEWED_MARKER, available_export, render_run_export,
)
from app.report_harness.release_registry import (
    ReleaseBinding, binding_status, export_eligibility,
)
from app.services.report_export_service import ReportExportService
from app.services.report_run_service import ReportRunService
from tests.harness_fixtures import SyntheticRoles, dependencies
from tests.test_run_recovery import _api_client


class HistoricalRegistry:
    def __init__(self, entries=()):
        self.entries = list(entries)

    def status(self, binding):
        return binding_status(self.entries, binding)


def historical_report():
    binding = ReleaseBinding(
        code_digest="a" * 64, policy_digest="b" * 64, model_endpoint_digest="c" * 64,
        knowledge_manifest_digest="d" * 64, evaluation_evidence_digest="e" * 64,
        approved_by="仅测试的历史绑定", approved_at="2026-09-17T00:00:00Z",
    )
    record = {
        "run_id": str(uuid4()), "state": "published", "quality_gate": "quality_validated",
        "execution_profile": "outbound", "release_binding": binding.model_dump(mode="json"),
        "report": {"report_markdown": "# 合成事故报告\n\n仅供测试的正文。\n\n" +
                   "\n\n".join(f"## 记录{i}\n\n合成事实与引用。" for i in range(20))},
    }
    return record, HistoricalRegistry([binding])


@pytest.mark.parametrize("export_format", ["md", "docx", "pdf"])
def test_engineering_exports_have_non_optional_markers(export_format):
    record, registry = historical_report()
    content, _, filename = render_run_export(
        record, export_format, mode="engineering", registry=registry,
        renderer=ReportExportService(SimpleNamespace()),
    )
    assert "engineering" in filename
    if export_format == "md":
        assert content.decode("utf-8").startswith(f"> {ENGINEERING_MARKER}")
    elif export_format == "docx":
        with ZipFile(BytesIO(content)) as archive:
            assert ENGINEERING_MARKER in archive.read("word/document.xml").decode("utf-8")
            assert ENGINEERING_MARKER in archive.read("word/header1.xml").decode("utf-8")
    else:
        reader = PdfReader(BytesIO(content))
        assert len(reader.pages) >= 3
        for page in reader.pages:
            assert ENGINEERING_MARKER in page.extract_text()


def test_revocation_blocks_formal_without_changing_history():
    record, registry = historical_report()
    before = deepcopy(record)
    renderer = ReportExportService(SimpleNamespace())
    assert export_eligibility(record, registry) == (True, "approved")
    content, _, filename = render_run_export(
        record, "md", mode="formal", registry=registry, renderer=renderer,
    )
    assert ENGINEERING_MARKER not in content.decode("utf-8")
    assert "engineering" not in filename
    registry.entries[0] = registry.entries[0].model_copy(update={
        "revoked_at": registry.entries[0].approved_at, "revocation_reason": "测试撤销",
    })
    assert export_eligibility(record, registry) == (False, "revoked")
    with pytest.raises(HarnessError, match="release_binding_revoked"):
        render_run_export(record, "md", mode="formal", registry=registry, renderer=renderer)
    assert record == before
    assert ENGINEERING_MARKER.encode() in render_run_export(
        record, "md", mode="engineering", registry=registry, renderer=renderer,
    )[0]


def test_synthetic_cannot_gain_formal_export_from_matching_binding():
    record, registry = historical_report()
    record["execution_profile"] = "synthetic_test"
    assert export_eligibility(record, registry) == (False, "unapproved")
    with pytest.raises(HarnessError, match="release_binding_revoked"):
        render_run_export(
            record, "md", mode="formal", registry=registry,
            renderer=ReportExportService(SimpleNamespace()),
        )


def test_dev_historical_formal_branch_still_forces_engineering_mark():
    record, registry = historical_report()
    content, _, filename = render_run_export(
        record, "md", mode="formal", registry=registry, force_engineering=True,
        renderer=ReportExportService(SimpleNamespace()),
    )
    assert ENGINEERING_MARKER.encode() in content and "engineering" in filename


@pytest.mark.parametrize("state", ["queued", "checking", "needs_review", "suspended", "cancelled", "failed"])
def test_unpublished_never_exports_even_as_engineering(state):
    record, registry = historical_report()
    record["state"] = state
    with pytest.raises(HarnessError, match="report_not_published"):
        render_run_export(
            record, "md", mode="engineering", registry=registry,
            renderer=ReportExportService(SimpleNamespace()),
        )


def test_http_owned_list_and_export_follow_real_publication(pg_store):
    store, owner, other, session = pg_store
    service = ReportRunService(store, dependencies(SyntheticRoles()))
    with _api_client(pg_store, service) as (client, headers):
        created = client.post("/api/v1/report-runs", headers=headers, json={
            "request_id": str(uuid4()), "session_id": session,
            "accident_data": {"事实": "浏览器导出合成案例"}, "evidence_revision": 0,
        }).json()
        run_id = created["run_id"]
        path = f"/api/v1/report-runs/{run_id}"
        assert client.get(f"{path}/exports/md?mode=engineering", headers=headers).status_code == 409
        response = client.post(f"{path}/execute/stream", headers=headers,
                               json={"expected_version": created["state_version"]})
        assert response.status_code == 200
        assert client.get(path, headers=headers).json()["state"] == "published"
        page = client.get("/api/v1/report-runs", headers=headers,
                          params={"session_id": session, "limit": 1}).json()
        assert [item["run_id"] for item in page["runs"]] == [run_id]
        assert page["next_cursor"] is None
        assert not page["runs"][0]["formal_export_eligible"]
        assert client.get(f"{path}/exports/md", headers=headers).status_code == 409
        for extension in ("md", "docx", "pdf"):
            exported = client.get(f"{path}/exports/{extension}?mode=engineering", headers=headers)
            assert exported.status_code == 200, exported.text
            assert "engineering" in exported.headers["content-disposition"]
            assert exported.headers["cache-control"] == "no-store"
        missing = client.get("/api/v1/report-runs", headers=headers,
                             params={"session_id": "another-session"})
        assert missing.status_code == 404
        invalid = client.get("/api/v1/report-runs", headers=headers,
                             params={"session_id": session, "cursor": str(uuid4())})
        assert invalid.status_code == 422


def test_http_historical_revocation_updates_view_and_blocks_download(pg_store):
    store, owner, other, session = pg_store
    record, registry = historical_report()
    record.update({
        "session_id": session, "state_version": 1, "snapshot_digest": "f" * 64,
        "candidate_version": 1, "review_status": "passed", "terminal_reason": None,
        "last_event_seq": 1, "budget": {}, "formal_export_eligible": True,
        "release_binding_status": "approved",
    })

    class ReadOnlyHistoricalStore:
        def get(self, requested_owner, run_id):
            if requested_owner != owner or run_id != record["run_id"]:
                raise HarnessError("not_found", 404)
            return deepcopy(record)

    from dataclasses import replace
    service = ReportRunService(
        ReadOnlyHistoricalStore(),
        replace(dependencies(SyntheticRoles()), release_registry=registry,
                force_engineering_exports=True),
    )
    with _api_client(pg_store, service) as (client, headers):
        path = f"/api/v1/report-runs/{record['run_id']}"
        assert client.get(path, headers=headers).json()["formal_export_eligible"]
        exported = client.get(f"{path}/exports/md", headers=headers)
        assert exported.status_code == 200
        assert ENGINEERING_MARKER in exported.text
        registry.entries.clear()
        view = client.get(path, headers=headers).json()
        assert not view["formal_export_eligible"]
        assert view["quality_gate"] == "quality_validated" and view["state"] == "published"
        assert client.get(f"{path}/exports/md", headers=headers).status_code == 409
        assert client.get(f"{path}/exports/md?mode=engineering", headers=headers).status_code == 200


def unreviewed_run():
    record, registry = historical_report()
    record.update(state="needs_review", quality_gate="engineering_only", release_binding=None,
                  candidate={"report_markdown": "# 合成候选稿\n\n独立审查尚未闭合。"})
    record.pop("report")
    return record, registry


@pytest.mark.parametrize("export_format", ["md", "docx", "pdf"])
def test_unreviewed_candidate_exports_only_as_marked_engineering(export_format):
    record, registry = unreviewed_run()
    renderer = ReportExportService(SimpleNamespace())
    content, _, filename = render_run_export(
        record, export_format, mode="engineering", registry=registry, renderer=renderer,
    )
    assert "unreviewed" in filename
    if export_format == "md":
        text = content.decode("utf-8")
        assert text.startswith(f"> {UNREVIEWED_MARKER}") and "合成候选稿" in text
    elif export_format == "docx":
        with ZipFile(BytesIO(content)) as archive:
            assert UNREVIEWED_MARKER in archive.read("word/header1.xml").decode("utf-8")
    else:
        for page in PdfReader(BytesIO(content)).pages:
            assert UNREVIEWED_MARKER in page.extract_text()
    with pytest.raises(HarnessError, match="report_not_published"):
        render_run_export(record, export_format, mode="formal", registry=registry, renderer=renderer)


def test_available_export_is_single_source_for_view_and_download():
    record, registry = historical_report()
    assert available_export(record, registry) == "formal"
    assert available_export(record, registry, force_engineering=True) == "engineering"
    assert available_export(record, HistoricalRegistry()) == "engineering"
    unreviewed, _ = unreviewed_run()
    assert available_export(unreviewed, registry) == "unreviewed"
    unreviewed.pop("candidate")
    assert available_export(unreviewed, registry) is None
    record["state"] = "failed"
    assert available_export(record, registry) is None


def test_http_unreviewed_run_view_and_download(pg_store):
    store, owner, other, session = pg_store
    service = ReportRunService(store, dependencies(SyntheticRoles(invalid_review=True)))
    with _api_client(pg_store, service) as (client, headers):
        created = client.post("/api/v1/report-runs", headers=headers, json={
            "request_id": str(uuid4()), "session_id": session,
            "accident_data": {"事实": "审查未闭合合成案例"}, "evidence_revision": 0,
        }).json()
        path = f"/api/v1/report-runs/{created['run_id']}"
        client.post(f"{path}/execute/stream", headers=headers,
                    json={"expected_version": created["state_version"]})
        view = client.get(path, headers=headers).json()
        assert view["state"] == "needs_review" and view["export_kind"] == "unreviewed"
        assert client.get(f"{path}/exports/md", headers=headers).status_code == 409
        exported = client.get(f"{path}/exports/pdf?mode=engineering", headers=headers)
        assert exported.status_code == 200 and "unreviewed" in exported.headers["content-disposition"]
