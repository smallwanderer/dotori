from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from llm_installation.function_catalog import (
    FunctionCapabilitySpec,
    FunctionEvidenceRecord,
    load_function_evidence,
    validate_function_evidence_target,
)


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def clear_evidence_cache():
    load_function_evidence.cache_clear()
    yield
    load_function_evidence.cache_clear()


def _evidence_payload(**overrides) -> dict:
    payload = {
        "schema_version": 1,
        "evidence_id": "ev-text-generation-v1",
        "function_id": "text.generate",
        "verification": "quality_verified",
        "model_id": "example-model",
        "model_revision": "model-revision-123",
        "contract_version": "text-generate-v1",
        "targets": [
            {
                "target_id": "example-artifact",
                "target_revision": "artifact-revision-123",
                "artifact_digest": "a" * 64,
                "backend_id": "llamacpp-cpu",
                "backend_version": "llama.cpp-1.0",
                "adapter_contract": "openai-chat@1",
            }
        ],
        "input_preprocessing_version": "input-v1",
        "output_parsing_version": "output-v1",
        "inference_options": {"temperature": 0},
        "fixture_set_id": "text-generation-smoke",
        "fixture_set_revision": "fixtures-v1",
        "evaluator_version": "evaluator-v1",
        "executed_at": "2026-09-30T00:00:00Z",
        "environment": {"platform": "test"},
        "sample_count": 3,
        "contract_checks": {"request": True, "response": True},
        "metrics": {"task_success_rate": 1.0},
        "failures": [],
        "acceptance_profile_id": "text-generation-smoke",
        "acceptance_profile_revision": "criteria-v1",
        "thresholds": {"task_success_rate": {"operator": "gte", "value": 1.0}},
        "passed": True,
    }
    payload.update(overrides)
    return payload


def _write_evidence(tmp_path, monkeypatch, payload=None):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(
        "llm_installation.function_catalog.EVIDENCE_DIR", evidence_dir
    )
    record = payload or _evidence_payload()
    (evidence_dir / f"{record['evidence_id']}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    return record["evidence_id"]


def test_quality_evidence_requires_metrics_to_meet_versioned_thresholds():
    record = FunctionEvidenceRecord.model_validate(_evidence_payload())

    assert record.verification == "quality_verified"
    assert record.sample_count == 3

    payload = _evidence_payload(metrics={"task_success_rate": 0.99})
    with pytest.raises(ValidationError, match="did not meet threshold"):
        FunctionEvidenceRecord.model_validate(payload)


def test_verified_function_requires_evidence_and_rejects_declaration_ref():
    with pytest.raises(ValidationError, match="requires an applicable evidence_ref"):
        FunctionCapabilitySpec(
            function_id="text.generate",
            verification="contract_verified",
            adapter_contract="openai-chat@1",
        )

    with pytest.raises(ValidationError, match="must not include declaration_ref"):
        FunctionCapabilitySpec(
            function_id="text.generate",
            verification="contract_verified",
            adapter_contract="openai-chat@1",
            evidence_ref="ev-text-generation-v1",
            declaration_ref="operator-config-v1",
        )


def test_evidence_reference_loads_and_matches_exact_model_target_and_backend(
    tmp_path, monkeypatch
):
    evidence_ref = _write_evidence(tmp_path, monkeypatch)
    function = FunctionCapabilitySpec(
        function_id="text.generate",
        verification="quality_verified",
        adapter_contract="openai-chat@1",
        artifact_ids=["example-artifact"],
        backend_ids=["llamacpp-cpu"],
        evidence_ref=evidence_ref,
    )

    validate_function_evidence_target(
        function,
        model_id="example-model",
        model_revision="model-revision-123",
        target_id="example-artifact",
        target_revision="artifact-revision-123",
        artifact_digest="a" * 64,
        backend_id="llamacpp-cpu",
    )

    with pytest.raises(ValueError, match="does not cover target"):
        validate_function_evidence_target(
            function,
            model_id="example-model",
            model_revision="model-revision-123",
            target_id="example-artifact",
            target_revision="artifact-revision-123",
            artifact_digest="a" * 64,
            backend_id="vllm-cuda",
        )


def test_verified_function_rejects_missing_or_mismatched_evidence(
    tmp_path, monkeypatch
):
    evidence_ref = _write_evidence(tmp_path, monkeypatch)
    function = FunctionCapabilitySpec(
        function_id="text.generate",
        verification="quality_verified",
        adapter_contract="openai-chat@1",
        evidence_ref=evidence_ref,
    )

    with pytest.raises(ValueError, match="does not match function/model revision"):
        validate_function_evidence_target(
            function,
            model_id="different-model",
            model_revision="model-revision-123",
            target_id="example-artifact",
            target_revision="artifact-revision-123",
            artifact_digest="a" * 64,
            backend_id="llamacpp-cpu",
        )

    missing = FunctionCapabilitySpec(
        function_id="text.generate",
        verification="contract_verified",
        adapter_contract="openai-chat@1",
        evidence_ref="missing-record",
    )
    with pytest.raises(ValueError, match="does not exist"):
        validate_function_evidence_target(
            missing,
            model_id="example-model",
            model_revision="model-revision-123",
            target_id="example-artifact",
            target_revision="artifact-revision-123",
            artifact_digest="a" * 64,
            backend_id="llamacpp-cpu",
        )
