import pytest

from app.report_harness.authorization import (
    AuthorizationCatalog, AuthorizationRequest, EndpointDescription,
)
from app.report_harness.contracts import canonical_digest
from app.report_harness.errors import HarnessError


def catalog(approved=True):
    return AuthorizationCatalog([
        EndpointDescription(role=role, label="合成端点", base_url="https://example.invalid/v1",
                            model="synthetic-model", version="synthetic-v1")
        for role in ("generator", "reviewer")
    ], [], frozenset({canonical_digest([])}) if approved else frozenset())


def record(policy):
    return {
        "snapshot_digest": canonical_digest({"text": "合成文字"}),
        "endpoint_profile_digest": policy.endpoint_digest,
        "snapshot": {"accident_data": {"text": "合成文字"},
                     "knowledge_manifest_digest": policy.knowledge_digest},
    }


def request(policy, item, **overrides):
    return AuthorizationRequest(**{
        "snapshot_digest": item["snapshot_digest"],
        "endpoint_profile_digest": policy.endpoint_digest,
        "approved_knowledge_manifest_digest": policy.knowledge_digest,
        "confirmed": True, **overrides,
    })


def test_approval_requires_all_three_matching_digests():
    policy = catalog()
    item = record(policy)
    assert policy.validate(item, request(policy, item))["snapshot_digest"] == item["snapshot_digest"]
    for name in ("snapshot_digest", "endpoint_profile_digest", "approved_knowledge_manifest_digest"):
        with pytest.raises(HarnessError, match="authorization_digest_conflict"):
            policy.validate(item, request(policy, item, **{name: "0" * 64}))


def test_unapproved_knowledge_manifest_is_unavailable():
    policy = catalog(approved=False)
    item = record(policy)
    assert not policy.preview(item)["available"]
    with pytest.raises(HarnessError, match="authorization_profile_unavailable"):
        policy.validate(item, request(policy, item))


def test_no_credential_fields_are_accepted():
    with pytest.raises(ValueError):
        EndpointDescription(role="generator", label="合成", base_url="https://u:p@example.invalid",
                            model="m", version="v")
    with pytest.raises(ValueError):
        EndpointDescription(role="generator", label="合成", base_url="https://example.invalid",
                            model="m", version="v", api_key="not-a-real-key")


def test_preview_is_not_a_mutable_reference_to_frozen_text():
    policy = catalog()
    item = record(policy)
    policy.preview(item)["snapshot"]["accident_data"]["text"] = "变更"
    assert item["snapshot"]["accident_data"]["text"] == "合成文字"
