"""Offline evidence applicability, separate from catalog identity and live health.

Callers must supply settings observed at the executing backend. This module does
not obtain versions from the evaluator container or copy them from evidence.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from llm_installation.function_catalog import FunctionEvidenceRecord


class ObservedFunctionExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    observation_ref: str = Field(min_length=1)
    runtime_generation_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    target_revision: str = Field(min_length=1)
    artifact_digest: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    backend_version: str = Field(min_length=1)
    adapter_contract: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    input_preprocessing_version: str = Field(min_length=1)
    output_parsing_version: str = Field(min_length=1)
    inference_options: dict = Field(default_factory=dict)


# These are fixture/transport/provenance metadata, not generation settings.
# All other keys require observed equality or a range supplied by the evidence.
_NON_EXECUTION_OPTIONS = frozenset({"request_timeout_seconds", "tested_token_cap", "runtime_fingerprint"})


def assess_evidence_applicability(record: FunctionEvidenceRecord, observed: ObservedFunctionExecution | None,
                                  *, expected_runtime_generation_id: str) -> dict:
    profile = record.applicability_profile
    result = {"evidence_ref": record.evidence_id, "status": "revalidation_required",
              "reasons": [], "profile_id": profile.profile_id if profile else "legacy-exact-options",
              "profile_revision": profile.revision if profile else "1"}
    if observed is None:
        result["reasons"] = ["execution_observation_missing"]
        return result
    result["observation_ref"] = observed.observation_ref
    reasons = result["reasons"]
    if observed.runtime_generation_id != expected_runtime_generation_id:
        reasons.append("runtime_generation_mismatch")
    for key in ("model_id", "model_revision", "contract_version", "input_preprocessing_version", "output_parsing_version"):
        if getattr(observed, key) != getattr(record, key):
            reasons.append(f"{key}_mismatch")
    targets = [target for target in record.targets if all(
        getattr(target, key) == getattr(observed, key)
        for key in ("target_id", "target_revision", "artifact_digest", "backend_id", "backend_version", "adapter_contract"))]
    if not targets:
        reasons.append("backend_artifact_adapter_mismatch")
    constraints = profile.option_constraints if profile else {}
    required_keys = (set(record.inference_options) | set(constraints)) - _NON_EXECUTION_OPTIONS
    observed_keys = set(observed.inference_options) - _NON_EXECUTION_OPTIONS
    if observed_keys != required_keys:
        reasons.append("inference_option_keys_mismatch")
    for key in sorted(required_keys & observed_keys):
        value = observed.inference_options[key]
        if key in constraints:
            valid = constraints[key].accepts(value)
        elif key == "requests":
            supported = record.inference_options[key]
            valid = (isinstance(value, list) and bool(value) and isinstance(supported, list)
                     and all(item in supported for item in value))
        else:
            expected = record.inference_options[key]
            valid = type(value) is type(expected) and value == expected
        if not valid:
            reasons.append(f"inference_option_mismatch:{key}")
    if not reasons:
        result.update(status="applicable", verification=record.verification)
    return result
