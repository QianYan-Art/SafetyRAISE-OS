from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.core.settings import HybridRetrievalSettings, LocalJsonlRetrievalSettings, RetrievalSettings
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError
from app.report_harness.knowledge_assets import (
    file_digest, knowledge_asset_paths, load_knowledge_assets, source_text,
)
from app.report_harness.knowledge_bridge import KnowledgeBridge


def fixture_settings(tmp_path, monkeypatch):
    names = ("manifest", "chunks", "rules", "search_index", "dense_manifest", "dense_records",
             "dense_vectors")
    paths = {}
    for name in names:
        path = tmp_path / name
        path.write_text("合成资产：" + name, encoding="utf-8")
        paths[name] = path
    fields = (
        "sparse_top_k_chunks", "sparse_top_k_rules", "dense_top_k_chunks", "dense_top_k_rules",
        "rrf_merge_top_k", "rerank_top_k", "final_context_top_k", "max_context_chars",
    )
    hybrid = SimpleNamespace(
        **{name: 3 for name in fields},
        **{"agentic_" + name: 2 for name in fields[:-2]},
        **{name + "_path": paths[name] for name in names if name.startswith("dense")},
    )
    local = SimpleNamespace(
        **{name + "_path": paths[name] for name in names if not name.startswith("dense")},
        fallback_mock_on_error=True, watch_manifest_changes=True,
    )
    settings = SimpleNamespace(
        retrieval=SimpleNamespace(provider="hybrid_local", local_jsonl=local, hybrid=hybrid),
        models=SimpleNamespace(
            retrieval_reranker=SimpleNamespace(enabled=False),
            retrieval_embedding=SimpleNamespace(model="synthetic", dimensions=2),
        ),
        resolve_path=lambda path: path,
    )
    settings.model_copy = lambda deep: deepcopy(settings)
    sparse = SimpleNamespace(
        **{name + "_path": paths[name] for name in names if not name.startswith("dense")},
        _ensure_loaded=lambda: None, _manifest={"catalog_version": "synthetic"},
        _chunk_records=[{"chunk_id": "chunk-1", "source_id": "source-1", "content": "合成知识"}],
        _rule_records=[],
    )
    monkeypatch.setattr("app.report_harness.knowledge_assets._build_local_jsonl_with_fallback",
                        lambda config: sparse)
    monkeypatch.setattr("app.report_harness.knowledge_assets.DenseIndexStore",
                        lambda **kwargs: SimpleNamespace(version="synthetic"))
    digest = canonical_digest({name: file_digest(path) for name, path in paths.items()})
    return settings, paths, digest, sparse


def test_only_approved_version_can_build_knowledge_context(tmp_path, monkeypatch):
    settings, paths, digest, _ = fixture_settings(tmp_path, monkeypatch)
    with pytest.raises(HarnessError, match="knowledge_source_unapproved"):
        load_knowledge_assets(settings, approved_content_digest="0" * 64)
    assets = load_knowledge_assets(settings, approved_content_digest=digest)
    assert assets.chunks[0]["id"] == "chunk-1"
    assert assets.collection.content_digest == digest
    assert assets.chunks[0]["manifest_digest"] == canonical_digest(
        [assets.collection.model_dump(mode="json")],
    )
    assets.validate()
    paths["rules"].write_text("合成改版规则", encoding="utf-8")
    with pytest.raises(HarnessError, match="authorization_stale"):
        assets.validate()


def test_resolved_source_substitution_is_rejected(tmp_path, monkeypatch):
    settings, paths, digest, sparse = fixture_settings(tmp_path, monkeypatch)
    sparse.rules_path = tmp_path / "other"
    with pytest.raises(HarnessError, match="knowledge_source_unapproved"):
        load_knowledge_assets(settings, approved_content_digest=digest)


def test_approval_hashes_actual_enhanced_rules_selected_by_original_retriever(tmp_path, monkeypatch):
    settings, paths, old_digest, sparse = fixture_settings(tmp_path, monkeypatch)
    enhanced = tmp_path / "liability_rules_enhanced.jsonl"
    enhanced.write_text("合成增强规则", encoding="utf-8")
    sparse.rules_path = enhanced
    selected = knowledge_asset_paths(settings)
    assert selected["rules"] == enhanced
    with pytest.raises(HarnessError, match="knowledge_source_unapproved"):
        load_knowledge_assets(settings, approved_content_digest=old_digest)
    digest = canonical_digest({name: file_digest(path) for name, path in selected.items()})
    assets = load_knowledge_assets(settings, approved_content_digest=digest)
    assert enhanced in assets.identities
    assert paths["rules"] not in assets.identities
    enhanced.write_text("合成增强规则已改变", encoding="utf-8")
    with pytest.raises(HarnessError, match="authorization_stale"):
        assets.validate()


def test_source_context_preserves_metadata_and_does_not_present_rules_as_statutes():
    record = {
        "rule_id": "source#rule#1", "content": "合成摘录，不是完整条文。",
        "authority": "合成机关", "effective_date": None,
        "citation": {"article": "合成条款"}, "raw_sha256": "a" * 64,
    }
    text = source_text(record, record["rule_id"], "source", ())
    assert "规则摘录，仅作为检索线索" in text
    assert '"effective_date": null' in text
    assert '"authority": "合成机关"' in text
    assert '"citation": {"article": "合成条款"}' in text
    assert '"raw_sha256": "' + "a" * 64 + '"' in text
    assert text.endswith(record["content"])


def test_source_neighbors_are_existing_adjacent_chunks_of_same_document(tmp_path, monkeypatch):
    settings, _, digest, sparse = fixture_settings(tmp_path, monkeypatch)
    sparse._chunk_records = [
        {"chunk_id": f"law#{number:04d}", "source_id": "law", "content": f"合成原文{number}"}
        for number in (1, 2, 4)
    ] + [{"chunk_id": "other#0003", "source_id": "other", "content": "其他来源"}]
    assets = load_knowledge_assets(settings, approved_content_digest=digest)
    assert "相邻来源片段：law#0002" in assets.chunks[0]["text"]
    assert "相邻来源片段：law#0001\n" in assets.chunks[1]["text"]
    assert "相邻来源片段：未提供" in assets.chunks[2]["text"]
    assert assets.chunks[0]["digest"] == canonical_digest(assets.chunks[0]["text"])


def test_rule_exposes_only_existing_body_chunks_from_its_own_source(tmp_path, monkeypatch):
    settings, _, digest, sparse = fixture_settings(tmp_path, monkeypatch)
    sparse._chunk_records = [
        {"chunk_id": "law#0002", "source_id": "law", "content": "完整条款与例外"},
        {"chunk_id": "law#0001", "source_id": "law", "content": "发布及施行信息"},
        {"chunk_id": "other#0001", "source_id": "other", "content": "其他来源"},
    ]
    sparse._rule_records = [
        {"rule_id": "law#rule#0001", "source_id": "law", "content": "规则摘录"},
        {"rule_id": "missing#rule#0001", "source_id": "missing", "content": "孤立摘录"},
    ]
    assets = load_knowledge_assets(settings, approved_content_digest=digest)
    text = assets.chunks[-2]["text"]
    assert "同来源正文索引（仅供定位，尚未读取）：law#0001、law#0002" in text
    assert "other#0001" not in text
    assert "law#rule#0001、" not in text
    assert "同来源正文索引（仅供定位，尚未读取）：未提供" in assets.chunks[-1]["text"]
    assert assets.chunks[-2]["digest"] == canonical_digest(text)


def test_real_sparse_dense_assets_feed_original_hybrid_retriever(tmp_path):
    """真实文件与原检索器；合成向量不证明嵌入模型的语义质量。"""
    names = ("manifest", "chunks", "rules", "search_index", "dense_manifest", "dense_records")
    paths = {name: tmp_path / (name + ".json") for name in names}
    paths["dense_vectors"] = tmp_path / "dense_vectors.npy"
    chunk = {
        "chunk_id": "synthetic-chunk", "source_id": "synthetic-source",
        "title": "合成驾驶资料", "content": "合成左转碰撞事实应核对来源。", "keywords": ["合成"],
    }
    rule = {
        "rule_id": "synthetic-rule", "source_id": "synthetic-rule-source",
        "title": "合成规则", "content": "合成左转碰撞仅用于工程验证。", "keywords": ["合成"],
    }
    values = {
        "manifest": {"catalog_version": "synthetic-real-files"},
        "chunks": chunk, "rules": rule, "search_index": {"indexes": {}},
        "dense_manifest": {"embedding_model": "synthetic-embedding", "version": "synthetic"},
    }
    for name, value in values.items():
        paths[name].write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    dense_records = [{
        "id": item.get("chunk_id") or item["rule_id"], "title": item["title"],
        "content": item["content"], "source": item["source_id"], "record_type": kind,
    } for kind, item in (("chunk", chunk), ("rule", rule))]
    paths["dense_records"].write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in dense_records), encoding="utf-8",
    )
    np.save(paths["dense_vectors"], np.asarray([[1, 0], [0.8, 0.6]], dtype=np.float16))
    local = LocalJsonlRetrievalSettings(
        manifest_path=str(paths["manifest"]), chunks_path=str(paths["chunks"]),
        rules_path=str(paths["rules"]), search_index_path=str(paths["search_index"]),
        prefer_enhanced_rules=False, fallback_mock_on_error=False,
    )
    hybrid = HybridRetrievalSettings(
        dense_manifest_path=str(paths["dense_manifest"]),
        dense_records_path=str(paths["dense_records"]),
        dense_vectors_path=str(paths["dense_vectors"]),
    )
    settings = SimpleNamespace(
        retrieval=RetrievalSettings(provider="hybrid_local", min_score=0, local_jsonl=local, hybrid=hybrid),
        models=SimpleNamespace(
            retrieval_reranker=SimpleNamespace(enabled=False),
            retrieval_embedding=SimpleNamespace(model="synthetic-embedding", dimensions=2),
        ),
        resolve_path=lambda value: Path(value),
    )
    settings.model_copy = lambda deep: deepcopy(settings)
    digest = canonical_digest({name: file_digest(path) for name, path in paths.items()})
    assets = load_knowledge_assets(settings, approved_content_digest=digest)
    queries = []

    def embed(query):
        queries.append(query)
        return [1.0, 0.0]

    retriever = assets.retriever(SimpleNamespace(embed_query=embed))
    bridge = KnowledgeBridge(retriever, assets.chunks, validate_source=assets.validate)
    result = bridge.initial("合成左转碰撞", 2)
    assert queries == ["合成左转碰撞"]
    assert {item["id"] for item in result} == {"synthetic-chunk", "synthetic-rule"}
    assert all(item["manifest_digest"] == assets.chunks[0]["manifest_digest"] for item in result)
    assert "合成左转碰撞" in result[0]["text"]
    assert isinstance(assets.dense.vectors, np.memmap)
    assert assets.dense.vectors.flags.writeable is False
    assert retriever.metadata["mode"] == "hybrid_rrf_only"
