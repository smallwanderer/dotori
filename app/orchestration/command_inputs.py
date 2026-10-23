"""Compile/pin consuming commands before transport, via declared domain ports."""
from dataclasses import asdict, replace
from copy import deepcopy

from django.db import transaction

from orchestration.artifact_bindings import resolve_artifact_binding
from orchestration.contracts import WorkContractError, validate_work_payload, validate_preparation_payload
from orchestration.handlers import get_plan_adapter
from orchestration.plans import command_for_step


def stored_command(row, plan, step):
    """Use the approved executable payload, never an unresolved plan template."""
    command = command_for_step(plan, step.step_id)
    if step.input_binding is None:
        return command
    record = row.control.get("compiled_commands", {}).get(step.step_id)
    if not isinstance(record, dict) or not isinstance(record.get("command"), dict):
        raise WorkContractError("Consuming command has not been durably compiled")
    compiled = record["command"]
    if ({key: value for key, value in compiled.items() if key != "payload"}
            != {key: value for key, value in asdict(command).items() if key != "payload"}):
        raise WorkContractError("Pinned command identity changed")
    if row.control.get("publication_mode") == "preparation":
        validate_preparation_payload(command.work_kind, compiled.get("payload"), phase="delivery")
    else:
        validate_work_payload(command.work_kind, compiled.get("payload"))
    return replace(command, payload=deepcopy(compiled["payload"]))


def _resolve(row, plan, step, adapter):
    from orchestration.submissions import live_reports
    binding = step.input_binding
    producer_job = None
    producing_report = None
    if binding.kind == "step":
        producing_report = next((report for report in live_reports(row, plan)
            if report.step_id == binding.reference and report.status == "succeeded"
            and report.artifact_refs), None)
        if producing_report is None:
            raise WorkContractError("Input producing dependency is not ready")
        producer_job = command_for_step(plan, binding.reference).job_id
    resolver = getattr(adapter, "resolve_input", None)
    if not callable(resolver):
        raise WorkContractError("Domain adapter cannot resolve approved inputs")
    artifact = resolve_artifact_binding(binding, source=plan.request.source,
        resolve_authorized_artifact=lambda value: resolver(plan, row, value,
            producer_job_id=producer_job), producer_job_id=producer_job)
    if producing_report is not None and artifact.artifact_id not in producing_report.artifact_refs:
        raise WorkContractError("Input artifact is outside the approved producing report")
    return artifact


def validate_compiled_input(row, plan, step, adapter):
    command = stored_command(row, plan, step)
    artifact = _resolve(row, plan, step, adapter)
    record = row.control["compiled_commands"][step.step_id]
    if record.get("input") != asdict(artifact):
        raise WorkContractError("Pinned command input changed; create a new request")
    validator = getattr(adapter, "validate_compiled_command", None)
    if not callable(validator):
        raise WorkContractError("Domain adapter cannot validate compiled commands")
    validator(plan, row, step, deepcopy(command.payload), artifact)
    return command


def prepare_step_command(plan_id, step_id):
    """Persist input and concrete payload in a transaction separate from send.

    Adapter compilation may save a domain manifest in this same transaction.
    Replay validates frozen payloads and never recompiles selector or revision.
    """
    from orchestration.submissions import submission_model, load_submission, validate_submission, live_reports
    with transaction.atomic():
        row = submission_model().objects.select_for_update().get(pk=plan_id)
        plan = load_submission(row)
        step = next(step for step in plan.steps if step.step_id == step_id)
        if step.input_binding is None:
            return command_for_step(plan, step_id)
        adapter = (get_plan_adapter(step.work_kind, preparation=True)
                   if row.control.get("publication_mode") == "preparation" else get_plan_adapter(step.work_kind))
        with adapter.source_guard(plan, row):
            validate_submission(row, plan)
            ready = {report.step_id for report in live_reports(row, plan)
                     if report.status == "succeeded" and report.artifact_refs}
            if set(step.depends_on) - ready:
                raise WorkContractError("Step dependencies are not ready")
            if step_id in row.control.get("compiled_commands", {}):
                return validate_compiled_input(row, plan, step, adapter)
            compiler = getattr(adapter, "compile_command", None)
            validator = getattr(adapter, "validate_compiled_command", None)
            if not callable(compiler) or not callable(validator):
                raise WorkContractError("Domain adapter cannot compile/validate consuming commands")
            artifact = _resolve(row, plan, step, adapter)
            payload = deepcopy(compiler(plan, row, step, artifact))
            if row.control.get("publication_mode") == "preparation":
                validate_preparation_payload(step.work_kind, payload, phase="delivery")
            else:
                validate_work_payload(step.work_kind, payload)
            validator(plan, row, step, deepcopy(payload), artifact)
            command = replace(command_for_step(plan, step_id), payload=payload)
            records = dict(row.control.get("compiled_commands", {}))
            records[step_id] = {"command": asdict(command), "input": asdict(artifact)}
            row.control = {**row.control, "compiled_commands": records}
            row.save(update_fields=["control", "updated_at"])
            return command
