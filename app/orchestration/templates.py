"""Static initial command composition, independent of domain storage."""
import time

from capabilities.functions import get_function
from capabilities.work_registry import get_work_definition
from orchestration.plans import ProcessingRequest, PlanStep, static_plan
from orchestration.plans import validate_plan_bindings
from capabilities.functions import COMPOSITION_DEFINITIONS


def bound_composition_plan(*, source, request_id, function_id, work_kind,
                           intent, input_binding, prerequisite_steps=(), deadline_at=None):
    """Non-publishing template; admission and domain validation are mandatory."""
    definition = COMPOSITION_DEFINITIONS[function_id]
    prerequisites = tuple(prerequisite_steps)
    dependencies = (input_binding.reference,) if input_binding.kind == "step" else ()
    consumer = PlanStep("embed", work_kind, function_id, definition.input_schema,
        definition.output_schema, intent, dependencies, input_binding)
    request = ProcessingRequest(request_id, source, (definition.output_schema,),
        time.time() + 1800 if deadline_at is None else deadline_at, len(prerequisites) + 1)
    plan = static_plan(request, (*prerequisites, consumer), version=2)
    validate_plan_bindings(plan)
    return plan


def initial_plan(*, source, request_id, work_kind, function_id, payload, deadline_at=None, step_id="initial-work"):
    get_work_definition(work_kind)
    function = get_function(function_id)
    request = ProcessingRequest(request_id, source, (function.output_schema,),
        time.time() + 1800 if deadline_at is None else deadline_at, 1)
    return static_plan(request, [PlanStep(step_id, work_kind, function_id,
        source.schema, function.output_schema, payload)])
