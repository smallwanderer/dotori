from types import SimpleNamespace
import copy

import pytest

from capabilities.function_scope import assess_request_options, project_request_scope
from documents.rag.request_evidence import audit_request_evidence
from llm_installation.function_catalog import load_function_evidence

pytestmark = pytest.mark.unit


@pytest.fixture
def scope():
    return project_request_scope(load_function_evidence(
        "text-generate-gemma3-qat-q4_0-llamacpp-cpu-20260930-command"))


def payload(**overrides):
    return {"model": "model", "messages": [{"role": "user", "content": "private prompt"}],
            "stream": True, "temperature": 0.0, "max_tokens": 16, **overrides}


def assess(scope, request):
    return assess_request_options(scope, request, adapter_contract="openai-compatible-chat-completions-v1")


def test_exact_final_options_and_numeric_temperature_match(scope):
    result = assess(scope, payload())
    assert result["status"] == "options_applicable"
    assert "private prompt" not in str(result)


@pytest.mark.parametrize("request_payload", [payload(max_tokens=512), payload(temperature=0.7), payload(top_p=0.9)])
def test_outside_scope_requires_revalidation_without_payload_storage(scope, request_payload):
    result = assess(scope, request_payload)
    assert result["status"] == "revalidation_required"
    assert "private prompt" not in str(result)
    assert "payload" not in result


def test_transport_usage_and_nonstream_subset_are_not_sampling_changes(scope):
    assert assess(scope, payload(stream=False, stream_options={"include_usage": True}))["status"] == "options_applicable"


def test_responses_adapter_not_in_chat_evidence(scope):
    request = payload()
    request["max_output_tokens"] = request.pop("max_tokens")
    assert assess(scope, request)["status"] == "options_applicable"
    result = assess_request_options(scope, request, adapter_contract="openai-responses-v1")
    assert "request_adapter_mismatch" in result["reasons"]


def test_versioned_evidence_range_only_allows_within_bounds(scope):
    scope["option_constraints"] = {"max_tokens": {"minimum": 8, "maximum": 32}}
    assert assess(scope, payload(max_tokens=32))["status"] == "options_applicable"
    assert assess(scope, payload(max_tokens=33))["status"] == "revalidation_required"
    assert assess(scope, payload(max_tokens=True))["status"] == "revalidation_required"


def test_legacy_scope_is_unavailable():
    assert assess(None, payload())["reasons"] == ["legacy_scope_unavailable"]


def config():
    return SimpleNamespace(provider_resolution={"source": "server_runtime", "deployment_generation_id": "plan-a",
        "model_installation_id": "llm:a", "verification": "contract_verified"})


def snapshot(monkeypatch, scope, generation="plan-a"):
    monkeypatch.setattr("documents.rag.request_evidence.load_deployment_snapshot", lambda: SimpleNamespace(
        generation_id=generation, function_providers={"text.generate": [{"model_installation_id": "llm:a", "request_evidence_scope": scope}]}))


def test_audit_records_outside_scope_without_changing_request_or_route(monkeypatch, scope):
    snapshot(monkeypatch, scope)
    selected = config()
    request = payload(max_tokens=512)
    original = copy.deepcopy(request)
    audit_request_evidence(selected, request, adapter_contract="openai-compatible-chat-completions-v1")
    assert request == original
    assert selected.provider_resolution["verification"] == "contract_verified"
    assert selected.provider_resolution["declaration_verification"] == "contract_verified"
    assert selected.provider_resolution["request_verification"] == "declared_unverified"
    assert selected.provider_resolution["request_evidence"]["mode"] == "audit"


def test_options_match_does_not_claim_backend_verified(monkeypatch, scope):
    snapshot(monkeypatch, scope)
    selected = config()
    audit_request_evidence(selected, payload(), adapter_contract="openai-compatible-chat-completions-v1")
    assert selected.provider_resolution["request_verification"] == "not_established"


def test_changed_generation_does_not_raise_or_reselect(monkeypatch, scope):
    snapshot(monkeypatch, scope, generation="changed")
    selected = config()
    audit_request_evidence(selected, payload(), adapter_contract="openai-compatible-chat-completions-v1")
    assert selected.provider_resolution["request_evidence"]["reasons"] == ["deployment_scope_unavailable"]
    assert selected.provider_resolution["model_installation_id"] == "llm:a"


def test_external_endpoint_policy_unchanged():
    selected = config()
    selected.provider_resolution["source"] = "workspace_external_endpoint"
    original = copy.deepcopy(selected.provider_resolution)
    audit_request_evidence(selected, payload(), adapter_contract="openai-compatible-chat-completions-v1")
    assert selected.provider_resolution == original
