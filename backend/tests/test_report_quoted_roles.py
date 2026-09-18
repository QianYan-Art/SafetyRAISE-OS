from copy import deepcopy
import json

import pytest

from app.report_harness.errors import HarnessError
from app.report_harness.transport_roles import RoleModel
from app.schemas.report_run import CandidateReport
from evals.report_harness.quoted_roles import QuotedTransportRoles, resolve_quotes


def test_exact_unicode_span_without_changing_report_or_evidence():
    candidate = {"report_markdown": "说明：\n车辆相撞。", "claims": [{
        "quote": "车辆相撞。", "claim_id": "c1", "type": "fact",
        "evidence_refs": ["e1"], "knowledge_refs": [],
    }]}
    original = deepcopy(candidate)
    result = resolve_quotes(candidate)
    assert result["claims"][0]["text_span"] == {"start": 4, "end": 9}
    assert result["report_markdown"] == candidate["report_markdown"]
    assert result["claims"][0]["evidence_refs"] == ["e1"]
    assert candidate == original


@pytest.mark.parametrize("text,quote", [("aaa", "aa"), ("甲甲", "甲"), ("甲", "乙"), ("甲", "")])
def test_missing_ambiguous_or_empty_quote_fails_closed(text, quote):
    with pytest.raises(HarnessError):
        resolve_quotes({"report_markdown": text, "claims": [{"quote": quote}]})


def test_generator_wire_schema_does_not_modify_core_contract():
    class Transport:
        output_limit = 100

    roles = QuotedTransportRoles(Transport(), {
        role: RoleModel("synthetic") for role in ("generator", "reviewer")
    })
    context = {"instructions": "合成协议", "response_schema": CandidateReport.model_json_schema()}
    original = deepcopy(context)
    _, payload = roles._payload("generator", context)
    wire = json.loads(payload["messages"][1]["content"])
    claim = wire["response_schema"]["$defs"]["Claim"]
    assert "quote" in claim["required"]
    assert "text_span" not in claim["properties"]
    assert context == original
    _, reviewer = roles._payload("reviewer", context)
    assert json.loads(reviewer["messages"][1]["content"])["response_schema"] == original["response_schema"]
