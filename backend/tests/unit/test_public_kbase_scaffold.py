import json
from pathlib import Path

from app.providers.retrieval.local_jsonl_retriever import LocalJsonlRetriever


DATA_DIR = Path(__file__).resolve().parents[3] / "examples" / "kbase" / "minimal" / "data"


def test_public_kbase_scaffold_contains_no_knowledge_records():
    manifest = json.loads((DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sources"] == []
    assert manifest["merged_chunk_records"] == 0
    assert manifest["merged_rule_records"] == 0
    assert not (DATA_DIR / "kbase_chunks.jsonl").read_text(encoding="utf-8").strip()
    assert not (DATA_DIR / "liability_rules.jsonl").read_text(encoding="utf-8").strip()
    index = json.loads((DATA_DIR / "search_index.json").read_text(encoding="utf-8"))
    assert index["indexes"] == {"chunk_inverted": {}, "rule_inverted": {}}


def test_public_kbase_scaffold_loads_but_cannot_supply_retrieval_results():
    retriever = LocalJsonlRetriever(
        manifest_path=DATA_DIR / "manifest.json",
        chunks_path=DATA_DIR / "kbase_chunks.jsonl",
        rules_path=DATA_DIR / "liability_rules.jsonl",
        search_index_path=DATA_DIR / "search_index.json",
        watch_manifest_changes=False,
    )
    assert retriever.metadata["retrieval_degraded"] is False
    assert retriever.search("路口事故责任", top_k=3) == []
