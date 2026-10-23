import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from pydantic import ValidationError

from llm_installation.function_applicability import ObservedFunctionExecution, assess_evidence_applicability
from llm_installation.function_catalog import FunctionEvidenceRecord, load_function_evidence

pytestmark = pytest.mark.unit


@pytest.fixture
def combination():
    record = load_function_evidence("text-generate-gemma3-qat-q4_0-llamacpp-cpu-20260930-command")
    target = record.targets[0]
    observed = ObservedFunctionExecution(
        observation_ref="synthetic-backend-test", runtime_generation_id="same-generation",
        model_id=record.model_id, model_revision=record.model_revision,
        target_id=target.target_id, target_revision=target.target_revision, artifact_digest=target.artifact_digest,
        backend_id=target.backend_id, backend_version=target.backend_version, adapter_contract=target.adapter_contract,
        contract_version=record.contract_version, input_preprocessing_version=record.input_preprocessing_version,
        output_parsing_version=record.output_parsing_version, inference_options=dict(record.inference_options))
    return record, observed


def assess(record, observed):
    return assess_evidence_applicability(record, observed, expected_runtime_generation_id="same-generation")


def test_exact_observed_combination_preserves_grade(combination):
    record, observed = combination
    result = assess(record, observed)
    assert result["status"] == "applicable"
    assert result["verification"] == "contract_verified"
    assert result["profile_id"] == "legacy-exact-options"


@pytest.mark.parametrize("field", ["runtime_generation_id", "model_id", "model_revision", "target_id",
    "target_revision", "artifact_digest", "backend_id", "backend_version", "adapter_contract",
    "contract_version", "input_preprocessing_version", "output_parsing_version"])
def test_changed_combination_requires_revalidation(combination, field):
    record, observed = combination
    result = assess(record, observed.model_copy(update={field: "different"}))
    assert result["status"] == "revalidation_required"
    assert "verification" not in result
    assert result["reasons"]


def test_missing_observation_cannot_be_filled_from_evidence(combination):
    result = assess(combination[0], None)
    assert result["reasons"] == ["execution_observation_missing"]


@pytest.mark.parametrize("options", [{"max_tokens": 512}, {"temperature": 0.5}, {"top_p": 0.9}])
def test_untested_options_require_revalidation(combination, options):
    record, observed = combination
    result = assess(record, observed.model_copy(update={"inference_options": {**observed.inference_options, **options}}))
    assert result["status"] == "revalidation_required"


def test_transport_timeout_and_single_tested_request_mode_do_not_invalidate(combination):
    record, observed = combination
    observed = observed.model_copy(update={"inference_options": {
        **observed.inference_options, "request_timeout_seconds": 20, "requests": ["stream"]}})
    assert assess(record, observed)["status"] == "applicable"


def test_only_evidence_supplied_versioned_range_allows_change(combination):
    record, observed = combination
    payload = record.model_dump()
    payload["applicability_profile"] = {"profile_id": "synthetic-reviewed-range", "revision": "2",
        "option_constraints": {"max_tokens": {"minimum": 8, "maximum": 32}}}
    ranged = FunctionEvidenceRecord.model_validate(payload)
    changed = observed.model_copy(update={"inference_options": {**observed.inference_options, "max_tokens": 24}})
    assert assess(record, changed)["status"] == "revalidation_required"
    assert assess(ranged, changed)["status"] == "applicable"
    too_large = changed.model_copy(update={"inference_options": {**changed.inference_options, "max_tokens": 33}})
    assert assess(ranged, too_large)["status"] == "revalidation_required"


def test_bool_is_not_numeric_range_value(combination):
    record, observed = combination
    payload = record.model_dump()
    payload["applicability_profile"] = {"profile_id": "range", "revision": "1",
        "option_constraints": {"max_tokens": {"minimum": 0, "maximum": 32}}}
    record = FunctionEvidenceRecord.model_validate(payload)
    observed = observed.model_copy(update={"inference_options": {**observed.inference_options, "max_tokens": True}})
    assert assess(record, observed)["status"] == "revalidation_required"


@pytest.mark.parametrize("constraint", [{"minimum": 32, "maximum": 8}, {},
    {"allowed_values": []}, {"allowed_values": [1], "minimum": 0, "maximum": 2}])
def test_invalid_profile_rejected(combination, constraint):
    payload = combination[0].model_dump()
    payload["applicability_profile"] = {"profile_id": "range", "revision": "1",
        "option_constraints": {"max_tokens": constraint}}
    with pytest.raises(ValidationError):
        FunctionEvidenceRecord.model_validate(payload)


def test_command_records_missing_observation_and_does_not_overwrite(tmp_path, combination):
    result = tmp_path / "report.json"
    with pytest.raises(CommandError, match="revalidation"):
        call_command("assess_function_evidence", evidence_ref=combination[0].evidence_id,
                     runtime_generation="same-generation", result_output=str(result), stdout=StringIO())
    assert json.loads(result.read_text())["status"] == "revalidation_required"
    original = result.read_bytes()
    with pytest.raises(CommandError):
        call_command("assess_function_evidence", evidence_ref=combination[0].evidence_id,
                     runtime_generation="same-generation", result_output=str(result), stdout=StringIO())
    assert result.read_bytes() == original


@pytest.mark.parametrize("key", ["untested_option", "request_timeout_seconds"])
def test_profile_cannot_add_untested_option_or_transport_scope(combination, key):
    payload = combination[0].model_dump()
    payload["applicability_profile"] = {"profile_id": "invalid-scope", "revision": "1",
        "option_constraints": {key: {"minimum": 0, "maximum": 32}}}
    with pytest.raises(ValidationError, match="evaluated execution options"):
        FunctionEvidenceRecord.model_validate(payload)


def test_same_generation_restart_preserves_assessment(combination):
    record, observed = combination
    fresh_observation = ObservedFunctionExecution.model_validate(observed.model_dump())
    assert assess(record, observed) == assess(record, fresh_observation)
