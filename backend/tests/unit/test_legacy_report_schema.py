import pytest
from pydantic import ValidationError

from app.schemas.workflow import GenerateReportRequest, GenerateReportResponse


@pytest.mark.parametrize("payload", [
    {"accident_data": {"事实": "合成事故"}},
    {"input_path": "synthetic.json"},
    {"video_path": "synthetic.mp4"},
])
def test_legacy_input_modes_remain_available(payload):
    assert GenerateReportRequest.model_validate(payload)


@pytest.mark.parametrize("payload", [
    {},
    {"accident_data": {}},
    {"accident_data": {"事实": "合成"}, "input_path": "synthetic.json"},
    {"video_path": "synthetic.mp4", "input_path": "synthetic.json"},
])
def test_legacy_input_rejections_are_preserved(payload):
    with pytest.raises(ValidationError):
        GenerateReportRequest.model_validate(payload)


def test_legacy_response_does_not_require_harness_fields():
    response = GenerateReportResponse.model_validate({
        "trace_id": "synthetic", "status": "success", "output_dir": "synthetic-output",
        "guidance": {}, "report": {"report_markdown": "合成报告"},
    })
    assert "run_id" not in response.model_dump()
