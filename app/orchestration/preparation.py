"""Pre-publication prerequisite decisions using owning-domain evidence.

This is a preparation boundary, not durable intake or permission to publish.
The adapter owns authorization and artifact semantics; orchestration owns the
choice to reuse input or compose a prerequisite. No executor is called here.
"""
from dataclasses import dataclass

from orchestration.contracts import WorkContractError


@dataclass(frozen=True)
class InputReadiness:
    ready: bool
    reason: str
    value: object = None


@dataclass(frozen=True)
class PreparedRequest:
    action: str
    reason: str
    value: object


def prepare_request(adapter):
    """Authorize/validate first; only explicit domain evidence permits parsing.

    Unexpected domain errors propagate rather than scheduling extra work.
    A pinned or scoped request may forbid prerequisites through its adapter.
    """
    context = adapter.validate_request()
    evidence = adapter.inspect_input(context)
    if (not isinstance(evidence, InputReadiness) or type(evidence.ready) is not bool
            or not isinstance(evidence.reason, str) or not evidence.reason):
        raise WorkContractError("Domain adapter returned invalid input evidence")
    if evidence.ready:
        return PreparedRequest("reuse", evidence.reason, adapter.compile_reuse(context, evidence.value))
    adapter.require_prerequisite_allowed(context, evidence)
    return PreparedRequest("prerequisite", evidence.reason, adapter.compose_prerequisite(context, evidence))


@dataclass(frozen=True)
class BoundPlanDescription:
    """Domain request data, not a plan or a decision to schedule work."""
    source: object
    request_id: str
    work_kind: str
    function_id: str
    intent: dict
    reusable_binding: object
    prerequisite_work_kind: str
    prerequisite_function_id: str
    prerequisite_payload: dict
    prerequisite_step_id: str = "initial-work"
    deadline_at: float | None = None


def prepare_bound_plan(adapter):
    """Common ownership of reuse/prerequisite selection and step composition."""
    from capabilities.functions import get_function
    from orchestration.artifact_bindings import ArtifactBinding
    from orchestration.plans import PlanStep
    from orchestration.templates import bound_composition_plan
    context = adapter.validate_request()
    evidence = adapter.inspect_input(context)
    if (not isinstance(evidence, InputReadiness) or type(evidence.ready) is not bool
            or not isinstance(evidence.reason, str) or not evidence.reason):
        raise WorkContractError("Domain adapter returned invalid input evidence")
    if not evidence.ready:
        adapter.require_prerequisite_allowed(context, evidence)
    description = adapter.describe_request(context, evidence)
    if not isinstance(description, BoundPlanDescription):
        raise WorkContractError("Domain adapter returned invalid request description")
    prerequisites = ()
    if evidence.ready:
        binding = description.reusable_binding
        if binding is None or binding.kind != "artifact":
            raise WorkContractError("Ready input requires an artifact binding")
    else:
        if description.reusable_binding is not None:
            raise WorkContractError("Unready input cannot provide a reusable binding")
        function = get_function(description.prerequisite_function_id)
        prerequisites = (PlanStep(description.prerequisite_step_id, description.prerequisite_work_kind,
            function.function_id, description.source.schema, function.output_schema,
            description.prerequisite_payload),)
        binding = ArtifactBinding("step", description.prerequisite_step_id, function.output_schema)
    return bound_composition_plan(source=description.source, request_id=description.request_id,
        function_id=description.function_id, work_kind=description.work_kind,
        intent=description.intent, input_binding=binding, prerequisite_steps=prerequisites,
        deadline_at=description.deadline_at)
