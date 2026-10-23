"""Compatibility exports; common implementation lives in orchestration."""
from capabilities.admission import CapabilityAdmissionError
from orchestration.contracts import (
    TASK_ENVELOPE_VERSION, TaskEnvelope, WorkContractError, derive_job_id,
    validate_work_payload, validate_followups,
)
from orchestration.dispatch import publish_registered_work
