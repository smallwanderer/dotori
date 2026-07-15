from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Mapping


HANDOFF_SCHEMA_VERSION = 1


class RuntimeHandoffError(ValueError):
    """Raised when Stage 5 output cannot become a valid Stage 7 input."""


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if hasattr(value, "as_dict"):
        return _json_value(value.as_dict())
    if is_dataclass(value):
        return _json_value(asdict(value))
    if hasattr(value, "__dict__"):
        return {
            key: _json_value(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    raise RuntimeHandoffError(
        f"Value of type {type(value).__name__} is not snapshot-serializable."
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _payload_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RuntimePolicyInput:
    schema_version: int
    artifact_id: str
    model_id: str
    runtime: str
    backend_profile: str
    fit_status: str
    selection_mode: str
    priority_preset: str
    selection_reason_code: str
    risky_confirmed: bool
    catalog_assessment_json: str
    hardware_profile_json: str
    integrity_sha256: str

    @property
    def catalog_assessment(self) -> dict[str, Any]:
        return json.loads(self.catalog_assessment_json)

    @property
    def hardware_profile(self) -> dict[str, Any]:
        return json.loads(self.hardware_profile_json)

    def _unsigned_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_id": self.artifact_id,
            "model_id": self.model_id,
            "runtime": self.runtime,
            "backend_profile": self.backend_profile,
            "fit_status": self.fit_status,
            "selection_mode": self.selection_mode,
            "priority_preset": self.priority_preset,
            "selection_reason_code": self.selection_reason_code,
            "risky_confirmed": self.risky_confirmed,
            "catalog_assessment": self.catalog_assessment,
            "hardware_profile": self.hardware_profile,
        }

    def verify_integrity(self) -> bool:
        return _payload_digest(self._unsigned_payload()) == self.integrity_sha256

    def as_dict(self) -> dict[str, Any]:
        return {
            **self._unsigned_payload(),
            "integrity_sha256": self.integrity_sha256,
        }


def build_runtime_policy_input(
    selection_result: Any,
    hardware_profile: Any,
) -> RuntimePolicyInput:
    if selection_result.selection_status != "SELECTED":
        raise RuntimeHandoffError("Stage 6 requires selection_status=SELECTED.")
    candidate = selection_result.selected_candidate
    if candidate is None:
        raise RuntimeHandoffError("Selected candidate is missing.")

    entry = candidate.entry
    assessment = candidate.assessment
    fit_evaluation = candidate.fit_evaluation
    fit_status = str(fit_evaluation.fit_status)
    if fit_status not in {"FIT", "RISKY"}:
        raise RuntimeHandoffError("Only FIT or confirmed RISKY may enter Stage 6.")
    if fit_status == "RISKY" and not selection_result.risky_confirmed:
        raise RuntimeHandoffError("RISKY selection is not confirmed.")
    assessment_fit_status = getattr(assessment, "fit_status", None)
    if assessment_fit_status is not None and str(assessment_fit_status) != fit_status:
        raise RuntimeHandoffError("Assessment and FitEvaluation status disagree.")
    assessment_artifact_id = getattr(assessment, "artifact_id", None)
    if assessment_artifact_id is not None and assessment_artifact_id != entry.id:
        raise RuntimeHandoffError("Assessment artifact_id does not match selection.")
    assessment_model_id = getattr(assessment, "model_id", None)
    if assessment_model_id is not None and assessment_model_id != entry.model_id:
        raise RuntimeHandoffError("Assessment model_id does not match selection.")
    backend_status = getattr(assessment, "backend_status", "AVAILABLE")
    if backend_status != "AVAILABLE":
        raise RuntimeHandoffError("Selected assessment backend is unavailable.")

    runtime = str(getattr(assessment, "runtime", "") or "")
    backend_profile = str(getattr(assessment, "backend_profile", "") or "")
    if not runtime or not backend_profile:
        raise RuntimeHandoffError("Selected assessment has no fixed runtime/backend.")
    runtime_by_backend = {
        "vllm-cuda": "vllm",
        "llamacpp-cpu": "llama.cpp",
        "llamacpp-gpu-offload": "llama.cpp",
    }
    if runtime_by_backend.get(backend_profile) != runtime:
        raise RuntimeHandoffError("Runtime and backend_profile are inconsistent.")
    declared_backends = set(getattr(entry, "backend_profiles", []) or [])
    if backend_profile not in declared_backends:
        raise RuntimeHandoffError("Selected backend is not declared by the artifact.")
    if getattr(assessment, "memory_placement", None) is None:
        raise RuntimeHandoffError("Selected assessment has no baseline placement.")

    assessment_snapshot = _json_value(assessment)
    assessment_snapshot["fit_status"] = fit_status
    assessment_snapshot["runtime"] = runtime
    assessment_snapshot["backend_profile"] = backend_profile
    assessment_snapshot["artifact_id"] = entry.id
    assessment_snapshot["model_id"] = entry.model_id
    hardware_snapshot = _json_value(hardware_profile)

    unsigned_payload = {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "artifact_id": entry.id,
        "model_id": entry.model_id,
        "runtime": runtime,
        "backend_profile": backend_profile,
        "fit_status": fit_status,
        "selection_mode": selection_result.selection_mode,
        "priority_preset": selection_result.priority_preset,
        "selection_reason_code": selection_result.reason_code,
        "risky_confirmed": bool(selection_result.risky_confirmed),
        "catalog_assessment": assessment_snapshot,
        "hardware_profile": hardware_snapshot,
    }
    return RuntimePolicyInput(
        schema_version=HANDOFF_SCHEMA_VERSION,
        artifact_id=entry.id,
        model_id=entry.model_id,
        runtime=runtime,
        backend_profile=backend_profile,
        fit_status=fit_status,
        selection_mode=selection_result.selection_mode,
        priority_preset=selection_result.priority_preset,
        selection_reason_code=selection_result.reason_code,
        risky_confirmed=bool(selection_result.risky_confirmed),
        catalog_assessment_json=_canonical_json(assessment_snapshot),
        hardware_profile_json=_canonical_json(hardware_snapshot),
        integrity_sha256=_payload_digest(unsigned_payload),
    )
