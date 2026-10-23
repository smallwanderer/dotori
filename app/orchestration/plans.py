"""Minimal static plans, validation and command/result linkage.

No queue consumer, scheduler or persistent workflow state is introduced here.
Plans are internal values; possession of a plan ID is not authorization.
"""
from dataclasses import dataclass
import math
import time
from uuid import uuid4

from capabilities.deployment import load_deployment_snapshot
from capabilities.functions import get_function, validate_function_contract, COMPOSITION_DEFINITIONS, validate_composition_provider
from capabilities.work_registry import require_registered_work
from orchestration.contracts import derive_job_id, validate_work_payload, validate_preparation_payload
from orchestration.artifact_bindings import ArtifactBinding, validate_artifact_binding, resolve_artifact_binding


class PlanValidationError(ValueError):
    pass


@dataclass(frozen=True)
class SourceReference:
    source_id: str
    revision: str
    schema: str
    checksum: str = ""


@dataclass(frozen=True)
class ProcessingRequest:
    request_id: str
    source: SourceReference
    required_schemas: tuple[str, ...]
    deadline_at: float
    max_steps: int = 8


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    work_kind: str
    function_id: str
    input_schema: str
    output_schema: str
    payload: dict
    depends_on: tuple[str, ...] = ()
    input_binding: ArtifactBinding | None = None


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    version: int
    request: ProcessingRequest
    steps: tuple[PlanStep, ...]
    deployment_generation: str


@dataclass(frozen=True)
class StepCommand:
    plan_id: str
    step_id: str
    job_id: str
    work_kind: str
    payload: dict
    deadline_at: float


@dataclass(frozen=True)
class StepReport:
    plan_id: str
    step_id: str
    job_id: str
    status: str
    artifact_refs: tuple[str, ...]


def static_plan(request, steps, *, plan_id=None, version=1):
    # Copy mutable payloads so the template's dict is not shared with a plan.
    from copy import deepcopy
    return ExecutionPlan(plan_id or str(uuid4()), version, request, tuple(deepcopy(steps)),
                         load_deployment_snapshot().generation_id)


def validate_plan_bindings(plan):
    """Version/lineage gate shared by validation and stored-record loading."""
    if type(plan.version) is not int or plan.version not in (1, 2):
        raise PlanValidationError("Unsupported plan version")
    bound = [step for step in plan.steps if step.input_binding is not None]
    if (plan.version == 1 and bound) or (plan.version == 2 and not bound):
        raise PlanValidationError("Artifact input bindings require a version 2 plan")
    seen = {}
    for step in plan.steps:
        binding = step.input_binding
        if binding is not None:
            validate_artifact_binding(binding)
            if binding.schema != step.input_schema:
                raise PlanValidationError("Binding schema does not match consuming step")
            if binding.kind == "step":
                producer = seen.get(binding.reference)
                if (producer is None or binding.reference not in step.depends_on
                        or producer.output_schema != binding.schema):
                    raise PlanValidationError("Input must reference an approved matching dependency")
        seen[step.step_id] = step


def validate_plan(plan, *, authorize_source, validate_domain_step, snapshot=None, now=None,
                  resolve_input=None, preparation=False):
    """Domain callbacks are mandatory; no default authorization bypass."""
    now = time.time() if now is None else now
    request = plan.request
    validate_plan_bindings(plan)
    if not plan.plan_id or not request.request_id:
        raise PlanValidationError("Unsupported plan version or missing identity")
    if not math.isfinite(request.deadline_at) or request.deadline_at <= now:
        raise PlanValidationError("Plan deadline exceeded")
    if type(request.max_steps) is not int or request.max_steps < 1 or not 0 < len(plan.steps) <= request.max_steps:
        raise PlanValidationError("Plan step budget exceeded")
    if not request.required_schemas or not request.source.source_id or not request.source.revision:
        raise PlanValidationError("Source revision and requested outputs are required")
    current = authorize_source(request.source)
    if current != request.source:
        raise PlanValidationError("Source identity or revision changed")
    snapshot = snapshot or load_deployment_snapshot()
    if snapshot.generation_id != plan.deployment_generation:
        raise PlanValidationError("Deployment changed since plan creation")
    seen = {}
    for step in plan.steps:
        if not step.step_id or step.step_id in seen:
            raise PlanValidationError("Step identities must be unique")
        if len(set(step.depends_on)) != len(step.depends_on) or any(dep not in seen for dep in step.depends_on):
            raise PlanValidationError("Dependencies must reference preceding distinct steps")
        composition = COMPOSITION_DEFINITIONS.get(step.function_id)
        definition = (validate_preparation_payload(step.work_kind, step.payload, phase="intent")
                      if preparation or (composition and step.input_binding is not None)
                      else validate_work_payload(step.work_kind, step.payload))
        registered = definition if preparation and composition else require_registered_work(step.work_kind)
        if registered != definition or not snapshot.is_enabled(definition.capability_id):
            raise PlanValidationError("Work capability is unavailable")
        function = composition or get_function(step.function_id)
        # Reuse the operator-selected, server-wide model-free role binding.
        # A declaration is not upgraded to a verified provider by this layer.
        binding = snapshot.role_bindings.get(definition.execution_role, {})
        provider_id = binding.get("provider_installation_id")
        if composition:
            if step.work_kind != composition.work_kind:
                raise PlanValidationError("Composition work does not match declaration")
            provider = validate_composition_provider(step.function_id, snapshot=snapshot,
                adapter_contract=composition.adapter_contract)
        else:
            provider = snapshot.resolve_function_provider(step.function_id,
                explicit_provider_installation_id=provider_id)
            validate_function_contract(step.function_id, provider)
        if (function.preferred_role != definition.execution_role
                or provider.get("role") != definition.execution_role
                or step.input_schema != function.input_schema
                or step.output_schema != function.output_schema):
            raise PlanValidationError("Function schemas or execution role do not match")
        if step.input_binding is not None:
            input_schemas = {step.input_binding.schema}
            if step.input_binding.kind == "artifact":
                if resolve_input is None:
                    raise PlanValidationError("Existing input requires an authorized domain resolver")
                resolve_artifact_binding(step.input_binding, source=request.source,
                    resolve_authorized_artifact=lambda binding: resolve_input(step, binding))
        else:
            input_schemas = {seen[dep].output_schema for dep in step.depends_on} if step.depends_on else {request.source.schema}
        if step.input_schema not in input_schemas:
            raise PlanValidationError("Step input schema has no matching source")
        validate_domain_step(step, request.source)
        seen[step.step_id] = step
    if not set(request.required_schemas).issubset({step.output_schema for step in plan.steps}):
        raise PlanValidationError("Plan does not provide requested output schemas")
    return plan


def command_for_step(plan, step_id):
    """Compile only; the execution boundary must revalidate before dispatch."""
    from copy import deepcopy
    step = next((item for item in plan.steps if item.step_id == step_id), None)
    if step is None:
        raise PlanValidationError("Unknown plan step")
    return StepCommand(plan.plan_id, step.step_id,
        derive_job_id(plan.plan_id, step.work_kind, step.step_id), step.work_kind,
        deepcopy(step.payload), plan.request.deadline_at)


def plan_record(plan):
    """Serializable metadata for an owning caller; not an approval token/store."""
    from dataclasses import asdict
    record = asdict(plan)
    if plan.version == 1:
        for step in record["steps"]:
            step.pop("input_binding", None)  # Preserve the historical v1 record shape.
    record["step_jobs"] = {step.step_id: command_for_step(plan, step.step_id).job_id for step in plan.steps}
    return record


def plan_from_record(record):
    """Reconstruct a server-owned record; still requires authorization."""
    try:
        request = record["request"]
        source = SourceReference(**request["source"])
        request = ProcessingRequest(request["request_id"], source, tuple(request["required_schemas"]),
                                    request["deadline_at"], request["max_steps"])
        steps = tuple(PlanStep(step["step_id"], step["work_kind"], step["function_id"],
            step["input_schema"], step["output_schema"], step["payload"], tuple(step["depends_on"]),
            ArtifactBinding(**step["input_binding"]) if step.get("input_binding") is not None else None)
            for step in record["steps"])
        plan = ExecutionPlan(record["plan_id"], record["version"], request, steps, record["deployment_generation"])
        validate_plan_bindings(plan)
        expected = {step.step_id: command_for_step(plan, step.step_id).job_id for step in steps}
        if record["step_jobs"] != expected:
            raise PlanValidationError("Stored step-job mapping changed")
        return plan
    except (KeyError, TypeError) as exc:
        raise PlanValidationError("Malformed plan record") from exc


def report_from_receipt(plan, step_id, receipt, artifacts=()):
    command = command_for_step(plan, step_id)
    if receipt.get("job_id") != command.job_id or receipt.get("work_kind") != command.work_kind:
        raise PlanValidationError("Receipt identity does not match plan step")
    step = next(item for item in plan.steps if item.step_id == step_id)
    refs = []
    for artifact in artifacts:
        source = artifact.get("source", {})
        if (artifact.get("schema") != step.output_schema or artifact.get("status") != "ready"
                or artifact.get("stale") or not artifact.get("id")
                or source.get("id") != plan.request.source.source_id
                or source.get("revision") != plan.request.source.revision
                or (plan.request.source.checksum and source.get("checksum") != plan.request.source.checksum)):
            raise PlanValidationError("Artifact schema, validity or source does not match")
        refs.append(artifact["id"])
    status = receipt.get("status")
    if status not in {"running", "succeeded", "failed"}:
        raise PlanValidationError("Unknown receipt state")
    if status == "succeeded" and not refs:
        raise PlanValidationError("Success receipt is not proof of a usable artifact")
    return StepReport(plan.plan_id, step_id, command.job_id, status, tuple(refs))


def requested_outputs_ready(plan, reports):
    """Only validated ready reports count; failed/missing steps are incomplete."""
    reports = tuple(reports)
    expected = {step.step_id: command_for_step(plan, step.step_id) for step in plan.steps}
    if len({report.step_id for report in reports}) != len(reports):
        raise PlanValidationError("Duplicate step report")
    for report in reports:
        command = expected.get(report.step_id)
        if command is None or report.plan_id != plan.plan_id or report.job_id != command.job_id:
            raise PlanValidationError("Report belongs to another plan or step")
    ready = {report.step_id for report in reports if report.status == "succeeded" and report.artifact_refs}
    if any(set(step.depends_on) - ready for step in plan.steps if step.step_id in ready):
        return False
    schemas = {step.output_schema for step in plan.steps if step.step_id in ready}
    return set(plan.request.required_schemas).issubset(schemas)
