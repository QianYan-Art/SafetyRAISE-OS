from pathlib import Path

from app.providers.retrieval.local_jsonl_retriever import GOVERNANCE_FIELDS, LocalJsonlRetriever


def _bare_retriever() -> LocalJsonlRetriever:
    # _to_result 只读入参，不依赖已加载的知识库文件。
    return LocalJsonlRetriever.__new__(LocalJsonlRetriever)


def test_sparse_result_carries_governance_fields_like_dense_hits():
    record = {
        "chunk_id": "source#0001", "source_id": "source", "title": "合成标题", "content": "合成正文",
        "category": "交通通告", "authority": "合成机关", "url": "https://example.invalid",
        "effect_level": "地方临时交通管理措施", "usage_note": "仅在事故地点与时间符合其适用范围时参考",
        "jurisdiction": "合成市", "effective_date": None, "published_date": "2026-01-01",
        "supersedes": [], "validity_note": "合成说明", "fetched_at": "2026-01-01T00:00:00+00:00",
    }
    result = _bare_retriever()._to_result(record, 0.5, "chunk")
    assert result["effect_level"] == "地方临时交通管理措施"
    assert result["jurisdiction"] == "合成市"
    assert result["published_date"] == "2026-01-01"
    assert result["validity_note"] == "合成说明"
    # 空值不进入提示词，抓取时间等内部字段也不透传。
    assert "effective_date" not in result
    assert "supersedes" not in result
    assert "fetched_at" not in result


def test_sparse_result_unchanged_for_records_without_governance_fields():
    record = {"rule_id": "source#rule#0001", "source_id": "source", "title": "合成", "content": "合成规则"}
    result = _bare_retriever()._to_result(record, 0.3, "rule")
    assert set(result) == {
        "id", "title", "content", "source", "score", "record_type", "citation", "url", "category", "authority",
    }
    assert not set(GOVERNANCE_FIELDS) & set(result)


def test_report_prompt_explains_governance_fields():
    root = Path(__file__).resolve().parents[2]
    text = (root / "config" / "分析报告生成提示词.md").read_text(encoding="utf-8")
    for field in ("effect_level", "usage_note", "jurisdiction", "effective_date", "supersedes"):
        assert f"`{field}`" in text
