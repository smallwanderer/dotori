from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from llm_installation.function_evaluator import evaluate_text_generate_contract


pytestmark = pytest.mark.unit


class FakeResponse:
    def __init__(self, *, status_code=200, payload=None, lines=()):
        self.status_code = status_code
        self._payload = payload
        self._lines = lines
        self.closed = False

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=True):
        yield from self._lines

    def close(self):
        self.closed = True


def _target_args():
    return {
        "base_url": "http://llm.local:8080/v1",
        "model": "example-artifact",
        "model_id": "example-model",
        "model_revision": "model-revision-123",
        "artifact_id": "example-artifact",
        "artifact_revision": "artifact-revision-123",
        "artifact_digest": "a" * 64,
        "backend_id": "llamacpp-cpu",
        "backend_version": "llama.cpp-test",
        "adapter_contract": "openai-chat@1",
        "evaluation_id": "ev-text-generate-test-1",
    }


def test_text_generate_contract_pass_emits_contract_evidence(monkeypatch):
    stream = FakeResponse(
        lines=[
            'data: {"choices":[{"delta":{"content":"DOTORI_"}}]}',
            'data: {"choices":[{"delta":{"content":"OK"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )
    responses = iter(
        [
            FakeResponse(
                payload={
                    "choices": [
                        {"message": {"content": "DOTORI_OK"}, "finish_reason": "stop"}
                    ]
                }
            ),
            stream,
        ]
    )
    calls = []

    def fake_post(url, *, json, timeout, **kwargs):
        calls.append((url, json, timeout, kwargs))
        return next(responses)

    monkeypatch.setattr("llm_installation.function_evaluator.requests.post", fake_post)

    result = evaluate_text_generate_contract(**_target_args())

    assert result.passed is True
    assert result.failures == ()
    assert result.evidence is not None
    assert result.evidence.verification == "contract_verified"
    assert result.evidence.targets[0].artifact_digest == "a" * 64
    assert result.evidence.environment["platform"]
    assert result.evidence.environment["python_version"]
    assert result.evidence.inference_options["request_timeout_seconds"] == 20
    assert [call[3].get("stream", False) for call in calls] == [False, True]
    assert all(call[0] == "http://llm.local:8080/v1/chat/completions" for call in calls)
    assert stream.closed is True
    assert "DOTORI_OK" not in json.dumps(result.as_dict()["metrics"])


def test_text_generate_contract_failure_emits_report_without_evidence(monkeypatch):
    responses = iter(
        [
            FakeResponse(payload={"choices": [{"message": {"content": "unrelated"}}]}),
            FakeResponse(
                lines=[
                    'data: {"choices":[{"delta":{"content":"DOTORI_OK"}}]}',
                    "data: [DONE]",
                ]
            ),
        ]
    )
    monkeypatch.setattr(
        "llm_installation.function_evaluator.requests.post",
        lambda *args, **kwargs: next(responses),
    )

    result = evaluate_text_generate_contract(**_target_args())

    assert result.passed is False
    assert result.evidence is None
    assert "nonstream_fixture_match" in result.failures
    assert result.as_dict()["evidence"] is None


def test_text_generate_contract_rejects_url_credentials_and_invalid_digest():
    args = _target_args()
    args["base_url"] = "http://user:secret@llm.local:8080"
    with pytest.raises(ValueError, match="without credentials"):
        evaluate_text_generate_contract(**args)

    args = _target_args()
    args["artifact_digest"] = "not-a-digest"
    with pytest.raises(ValueError, match="SHA-256"):
        evaluate_text_generate_contract(**args)


def test_management_command_writes_report_and_passing_evidence(
    tmp_path, monkeypatch
):
    import documents.management.commands.evaluate_text_generate as command_module

    entry = SimpleNamespace(
        id="example-artifact",
        model_id="example-model",
        hf=SimpleNamespace(revision="model-revision-123"),
        artifact=SimpleNamespace(sha256="a" * 64),
        backend_profiles=["llamacpp-cpu"],
    )
    evidence = SimpleNamespace(model_dump=lambda **kwargs: {"verification": "contract_verified"})
    result = SimpleNamespace(
        passed=True,
        evidence=evidence,
        as_dict=lambda: {"passed": True, "failures": []},
    )
    calls = {}
    monkeypatch.setattr(command_module, "get_catalog_entry", lambda artifact_id: entry)

    def fake_evaluate(**kwargs):
        calls.update(kwargs)
        return result

    monkeypatch.setattr(command_module, "evaluate_text_generate_contract", fake_evaluate)
    result_path = tmp_path / "reports" / "run.json"
    evidence_path = tmp_path / "evidence" / "ev-text-generate.json"

    call_command(
        "evaluate_text_generate",
        base_url="http://llm.local:8080",
        artifact_id=entry.id,
        backend_id="llamacpp-cpu",
        backend_version="llama.cpp-test",
        adapter_contract="openai-chat@1",
        evaluation_id="ev-text-generate",
        result_output=str(result_path),
        evidence_output=str(evidence_path),
        stdout=StringIO(),
    )

    report = json.loads(result_path.read_text(encoding="utf-8"))
    saved_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert report["target"]["catalog_model_id"] == "example-model"
    assert report["target"]["backend_id"] == "llamacpp-cpu"
    assert calls["artifact_digest"] == "a" * 64
    assert calls["timeout"] == 120
    assert saved_evidence["verification"] == "contract_verified"


def test_management_command_persists_failed_report_without_evidence(
    tmp_path, monkeypatch
):
    import documents.management.commands.evaluate_text_generate as command_module

    entry = SimpleNamespace(
        id="example-artifact",
        model_id="example-model",
        hf=SimpleNamespace(revision="model-revision-123"),
        artifact=SimpleNamespace(sha256="a" * 64),
        backend_profiles=["llamacpp-cpu"],
    )
    result = SimpleNamespace(
        passed=False,
        evidence=None,
        as_dict=lambda: {
            "passed": False,
            "failures": ["stream_done_received"],
            "evidence": None,
        },
    )
    monkeypatch.setattr(command_module, "get_catalog_entry", lambda artifact_id: entry)
    monkeypatch.setattr(
        command_module, "evaluate_text_generate_contract", lambda **kwargs: result
    )
    result_path = tmp_path / "reports" / "failed.json"
    evidence_path = tmp_path / "evidence" / "must-not-exist.json"

    with pytest.raises(CommandError, match="contract evaluation failed"):
        call_command(
            "evaluate_text_generate",
            base_url="http://llm.local:8080",
            artifact_id=entry.id,
            backend_id="llamacpp-cpu",
            backend_version="llama.cpp-test",
            adapter_contract="openai-chat@1",
            evaluation_id="ev-text-generate-failed",
            result_output=str(result_path),
            evidence_output=str(evidence_path),
            stdout=StringIO(),
        )

    report = json.loads(result_path.read_text(encoding="utf-8"))
    assert report["passed"] is False
    assert report["failures"] == ["stream_done_received"]
    assert not evidence_path.exists()
