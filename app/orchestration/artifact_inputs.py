"""Pin validated input evidence in the existing common submission journal.

This does not enable new steps or replace dependency authorization. New plan-v2
intake/compilation remains separate from the compatible v1 sidecar journal.
"""
from dataclasses import asdict
import hashlib
import json

from django.db import transaction

from orchestration.artifact_bindings import (
    BINDING_SCHEMA, validate_artifact_binding, resolve_artifact_binding,
)
from orchestration.contracts import WorkContractError
from orchestration.handlers import get_plan_adapter
from orchestration.plans import command_for_step
from orchestration.submissions import submission_model, load_submission, validate_submission, live_reports


def pin_submission_artifact(plan_id, binding):
    """Resolve via the declared owner, then freeze; replay never repins a revision."""
    validate_artifact_binding(binding)
    key = hashlib.sha256(json.dumps({"kind": binding.kind, "reference": binding.reference,
        "schema": binding.schema}, sort_keys=True).encode()).hexdigest()
    with transaction.atomic():
        row = submission_model().objects.select_for_update().get(pk=plan_id)
        plan = load_submission(row)
        validate_submission(row, plan)
        producer_job = None
        step = plan.steps[0]
        if binding.kind == "step":
            step = next((s for s in plan.steps if s.step_id == binding.reference), None)
            if step is None:
                raise WorkContractError("Input must reference an approved dependency step")
            reports = live_reports(row, plan)
            if not any(r.step_id == step.step_id and r.status == "succeeded" and r.artifact_refs for r in reports):
                raise WorkContractError("Input producing step is not ready")
            producer_job = command_for_step(plan, step.step_id).job_id
        adapter = get_plan_adapter(step.work_kind)
        resolver = getattr(adapter, "resolve_input", None)
        if resolver is None:
            raise WorkContractError("Declared domain adapter cannot resolve artifact inputs")
        with adapter.source_guard(plan, row):
            validate_submission(row, plan)
            artifact = resolver(plan, row, binding, producer_job_id=producer_job)
            artifact = resolve_artifact_binding(binding, source=plan.request.source,
                resolve_authorized_artifact=lambda _: artifact, producer_job_id=producer_job)
            record = {"schema": BINDING_SCHEMA, "binding": asdict(binding),
                      "artifact": asdict(artifact), "source": asdict(plan.request.source)}
            inputs = dict(row.control.get("artifact_inputs", {}))
            if key in inputs and inputs[key] != record:
                raise WorkContractError("Pinned input changed; create a new request")
            inputs[key] = record
            row.control = {**row.control, "artifact_inputs": inputs}
            row.save(update_fields=["control", "updated_at"])
    return record
