"""Model-independent schema for catalogued model functions.

Function declarations describe a model/provider combination. They do not
claim quality unless a versioned evaluation record supports that grade.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from llm_installation.function_applicability_profile import EvidenceApplicabilityProfile


VerificationGrade = Literal[
    "declared_unverified", "contract_verified", "quality_verified"
]
KNOWN_FUNCTION_IDS = frozenset(
    {"text.generate", "text.embed", "vision.ocr", "vision.caption"}
)
EVIDENCE_DIR = Path(__file__).parent / "evidence"
_EVIDENCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class FunctionEvidenceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(min_length=1)
    target_revision: str = Field(min_length=1)
    artifact_digest: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    backend_version: str = Field(min_length=1)
    adapter_contract: str = Field(min_length=1)


class EvaluationThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operator: Literal["gte", "lte"]
    value: float


class FunctionEvidenceRecord(BaseModel):
    """Versioned offline evidence used to substantiate catalog function claims."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    evidence_id: str = Field(min_length=1)
    function_id: str = Field(min_length=1)
    verification: Literal["contract_verified", "quality_verified"]
    model_id: str = Field(min_length=1)
    model_revision: str = Field(min_length=1)
    contract_version: str = Field(min_length=1)
    targets: list[FunctionEvidenceTarget] = Field(min_length=1)
    input_preprocessing_version: str = Field(min_length=1)
    output_parsing_version: str = Field(min_length=1)
    inference_options: dict = Field(default_factory=dict)
    applicability_profile: EvidenceApplicabilityProfile | None = None
    fixture_set_id: str = Field(min_length=1)
    fixture_set_revision: str = Field(min_length=1)
    evaluator_version: str = Field(min_length=1)
    executed_at: str = Field(min_length=1)
    environment: dict = Field(default_factory=dict)
    sample_count: int = Field(gt=0)
    contract_checks: dict[str, bool] = Field(min_length=1)
    metrics: dict[str, float] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    acceptance_profile_id: str | None = None
    acceptance_profile_revision: str | None = None
    thresholds: dict[str, EvaluationThreshold] = Field(default_factory=dict)
    passed: bool

    @model_validator(mode="after")
    def validate_evaluation_result(self) -> "FunctionEvidenceRecord":
        if not self.passed or self.failures or not all(self.contract_checks.values()):
            raise ValueError("function evidence must contain a passing result")
        if self.applicability_profile:
            keys = set(self.applicability_profile.option_constraints)
            if (not keys.issubset(self.inference_options)
                    or keys & {"request_timeout_seconds", "tested_token_cap", "runtime_fingerprint"}):
                raise ValueError("applicability ranges must refer to evaluated execution options")
        if self.verification == "quality_verified":
            if not self.acceptance_profile_id or not self.acceptance_profile_revision:
                raise ValueError("quality evidence requires a versioned acceptance profile")
            if not self.thresholds:
                raise ValueError("quality evidence requires metric thresholds")
            for metric, threshold in self.thresholds.items():
                if metric not in self.metrics:
                    raise ValueError(f"quality evidence is missing metric {metric}")
                score = self.metrics[metric]
                passed = (
                    score >= threshold.value
                    if threshold.operator == "gte"
                    else score <= threshold.value
                )
                if not passed:
                    raise ValueError(f"quality evidence did not meet threshold {metric}")
        return self


class FunctionCapabilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    function_id: str = Field(min_length=1)
    preferred_role: str | None = None
    verification: VerificationGrade
    artifact_ids: list[str] = Field(default_factory=list)
    backend_ids: list[str] = Field(default_factory=list)
    adapter_contract: str = Field(min_length=1)
    evidence_ref: str | None = None
    declaration_ref: str | None = None

    @model_validator(mode="after")
    def validate_evidence_reference(self) -> "FunctionCapabilitySpec":
        reference = self.evidence_ref if self.verification != "declared_unverified" else self.declaration_ref
        if reference and not _EVIDENCE_ID_PATTERN.fullmatch(reference):
            raise ValueError("evidence/declaration reference must be a safe identifier")
        return self

    @model_validator(mode="after")
    def validate_grade_evidence(self) -> "FunctionCapabilitySpec":
        if self.verification in {"contract_verified", "quality_verified"}:
            if not self.evidence_ref:
                raise ValueError(
                    f"{self.verification} requires an applicable evidence_ref"
                )
            if self.declaration_ref:
                raise ValueError("verified functions must not include declaration_ref")
        else:
            if not self.declaration_ref:
                raise ValueError("declared_unverified requires a declaration_ref")
            if self.evidence_ref:
                raise ValueError("declared_unverified must not include evidence_ref")
        if len(self.artifact_ids) != len(set(self.artifact_ids)):
            raise ValueError("artifact_ids must not contain duplicates")
        if len(self.backend_ids) != len(set(self.backend_ids)):
            raise ValueError("backend_ids must not contain duplicates")
        return self


def validate_function_list(functions: list[FunctionCapabilitySpec]) -> None:
    ids = [function.function_id for function in functions]
    unknown = set(ids) - KNOWN_FUNCTION_IDS
    if unknown:
        raise ValueError(f"Unknown function IDs: {', '.join(sorted(unknown))}")
    if len(ids) != len(set(ids)):
        raise ValueError("functions must not contain duplicate function_id values")


@lru_cache(maxsize=256)
def load_function_evidence(evidence_ref: str) -> FunctionEvidenceRecord:
    """Load a catalog evidence record by ID from the versioned evidence directory."""
    if not _EVIDENCE_ID_PATTERN.fullmatch(evidence_ref or ""):
        raise ValueError("Invalid function evidence reference")
    path = EVIDENCE_DIR / f"{evidence_ref}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        record = FunctionEvidenceRecord.model_validate(payload)
    except FileNotFoundError as exc:
        raise ValueError(f"Function evidence does not exist: {evidence_ref}") from exc
    except Exception as exc:
        raise ValueError(f"Invalid function evidence record: {evidence_ref}") from exc
    if record.evidence_id != evidence_ref:
        raise ValueError(f"Evidence ID does not match reference: {evidence_ref}")
    return record


def validate_function_evidence_target(
    function: FunctionCapabilitySpec,
    *,
    model_id: str,
    model_revision: str,
    target_id: str,
    target_revision: str,
    artifact_digest: str,
    backend_id: str,
) -> None:
    """Require verified evidence for the exact model/target/backend combination."""
    if function.verification == "declared_unverified":
        return
    record = load_function_evidence(function.evidence_ref or "")
    if (
        record.function_id != function.function_id
        or record.verification != function.verification
        or record.model_id != model_id
        or record.model_revision != model_revision
    ):
        raise ValueError(
            f"Evidence {record.evidence_id} does not match function/model revision."
        )
    target_matches = any(
        target.target_id == target_id
        and target.target_revision == target_revision
        and target.artifact_digest == artifact_digest
        and target.backend_id == backend_id
        and target.adapter_contract == function.adapter_contract
        for target in record.targets
    )
    if not target_matches:
        raise ValueError(
            f"Evidence {record.evidence_id} does not cover target {target_id} "
            f"with backend {backend_id} and adapter {function.adapter_contract}."
        )
