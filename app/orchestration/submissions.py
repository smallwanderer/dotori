"""Common bounded static-plan delivery; no scheduler service or dynamic graph."""
from dataclasses import asdict
import logging

from django.db import transaction, connection
from django.utils import timezone

from orchestration.contracts import WorkContractError, validate_followups
from orchestration.handlers import get_plan_adapter
from orchestration.plans import command_for_step, plan_record, plan_from_record, PlanValidationError
from orchestration.dispatch import publish_registered_work
from orchestration.control import decide_proposals, completion_candidate
from orchestration.command_inputs import prepare_step_command, stored_command, validate_compiled_input

logger = logging.getLogger(__name__)


def submission_model():
    from orchestration_state.models import PlanSubmission
    return PlanSubmission


def delivery_model():
    from orchestration_state.models import PlanCommandDelivery
    return PlanCommandDelivery


def load_submission(row):
    plan = plan_from_record(row.definition)
    if not plan.steps or len(plan.steps) > plan.request.max_steps:
        raise PlanValidationError("Plan is empty or exceeds its step budget")
    command = command_for_step(plan, plan.steps[0].step_id)
    if (plan.plan_id != row.plan_id or plan.request.request_id != row.request_id
            or command.job_id != row.job_id or plan.request.source.source_id != row.source_uid):
        raise PlanValidationError("Submission identity does not match the stored plan")
    preceding = set()
    for step in plan.steps:
        if not step.step_id or len(step.step_id) > 64 or step.step_id in preceding or set(step.depends_on) - preceding:
            raise PlanValidationError("Invalid step identity or dependency order")
        preceding.add(step.step_id)
    return plan


def validate_submission(row, plan):
    for kind in dict.fromkeys(step.work_kind for step in plan.steps):
        if row.control.get("publication_mode") == "preparation":
            adapter = get_plan_adapter(kind, preparation=True)
            adapter.validate_preparation(plan, row)
        else:
            get_plan_adapter(kind).validate(plan, row)


def parent_job(plan, step):
    return command_for_step(plan, step.depends_on[0]).job_id if step.depends_on else plan.plan_id


def approved_commands(row, plan):
    """Bound templates are not approved executable payloads until compiled."""
    return [stored_command(row, plan, step) for step in plan.steps
            if step.input_binding is None or step.step_id in row.control.get("compiled_commands", {})]


def live_reports(row, plan):
    reports = []
    for step in plan.steps:
        stored = row.reports.get(step.step_id)
        if stored is None:
            continue
        command = command_for_step(plan, step.step_id)
        if (stored.get("plan_id") != plan.plan_id or stored.get("step_id") != step.step_id
                or stored.get("job_id") != command.job_id):
            raise WorkContractError("Stored report identity is invalid")
        report = get_plan_adapter(step.work_kind).report(plan, row,
            {"job_id": command.job_id, "work_kind": step.work_kind, "status": stored["status"]})
        if (report.plan_id != plan.plan_id or report.step_id != step.step_id or report.job_id != command.job_id):
            raise WorkContractError("Adapter returned another step report")
        reports.append(report)
    return reports


def ready_steps(plan, reports):
    ready = {r.step_id for r in reports if r.status == "succeeded" and r.artifact_refs}
    reported = {r.step_id for r in reports}
    if completion_candidate(plan, reports) == "completed":
        return []
    return [step for step in plan.steps if step.step_id not in reported and set(step.depends_on) <= ready]


def submit_plan(plan, *, actor, workspace, deadline_at=None, preparation=False):
    Model = submission_model()
    request_id = plan.request.request_id
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 64:
        raise PlanValidationError("request_id must be a non-empty string up to 64 characters")
    if not plan.steps:
        raise PlanValidationError("Plan must contain a step")
    command = command_for_step(plan, plan.steps[0].step_id)
    with transaction.atomic():
        row, created = Model.objects.get_or_create(workspace_id=workspace.pk, request_id=request_id,
            defaults={"plan_id": plan.plan_id, "job_id": command.job_id,
                "created_by_id": actor.pk, "source_uid": plan.request.source.source_id,
                "definition": plan_record(plan),
                "control": {"publication_mode": "preparation"} if preparation else {}})
        if (row.control.get("publication_mode") == "preparation") != preparation:
            raise PlanValidationError("Request publication mode cannot change")
        if row.created_by_id != actor.pk or row.source_uid != plan.request.source.source_id:
            raise PlanValidationError("request_id was reused for another actor or source")
        existing = load_submission(row)
        if (existing.steps != plan.steps or existing.request.source != plan.request.source
                or existing.request.required_schemas != plan.request.required_schemas
                or existing.request.max_steps != plan.request.max_steps
                or existing.deployment_generation != plan.deployment_generation
                or existing.version != plan.version):
            raise PlanValidationError("request_id was reused for another plan")
        if deadline_at is not None and existing.request.deadline_at != deadline_at:
            raise PlanValidationError("request_id was reused with another deadline")
        validate_submission(row, existing)
        for step in existing.steps:
            delivery, _ = delivery_model().objects.get_or_create(plan=row, step_id=step.step_id,
                defaults={"job_id": command_for_step(existing, step.step_id).job_id})
            if delivery.job_id != command_for_step(existing, step.step_id).job_id:
                raise PlanValidationError("Delivery identity does not match the stored step")
        if not preparation and (created or row.published_at is None):
            transaction.on_commit(lambda: _publish_after_commit(row.plan_id))
    return row


def _publish_after_commit(plan_id):
    try:
        publish_submission(plan_id)
    except Exception:
        logger.exception("Plan %s remains available for publication recovery", plan_id)


def publish_step(plan_id, step_id, *, replay=False):
    Model = submission_model()
    try:
        initial_row = Model.objects.get(pk=plan_id)
        if initial_row.control.get("publication_mode") == "preparation":
            raise WorkContractError("Preparation plans cannot publish work")
        initial_plan = load_submission(initial_row)
        initial_step = next(step for step in initial_plan.steps if step.step_id == step_id)
        if initial_step.input_binding is not None:
            if connection.in_atomic_block:
                raise WorkContractError("Consuming commands must be committed before publication")
            prepare_step_command(plan_id, step_id)
        with transaction.atomic():
            row = Model.objects.select_for_update().get(pk=plan_id)
            plan = load_submission(row)
            step = next(s for s in plan.steps if s.step_id == step_id)
            delivery = delivery_model().objects.select_for_update().get(plan=row, step_id=step_id)
            if delivery.job_id != command_for_step(plan, step_id).job_id:
                raise PlanValidationError("Delivery identity mismatch")
            if delivery.published_at is not None and not replay:
                return row
            adapter = get_plan_adapter(step.work_kind)
            with adapter.source_guard(plan, row):
                validate_submission(row, plan)
                reports = live_reports(row, plan)
                current = next((r for r in reports if r.step_id == step_id), None)
                if current is not None and (current.status == "succeeded" or not replay):
                    return row
                ready = {r.step_id for r in reports if r.status == "succeeded" and r.artifact_refs}
                if set(step.depends_on) - ready:
                    raise WorkContractError("Step dependencies are not ready")
                command = (validate_compiled_input(row, plan, step, adapter)
                           if step.input_binding is not None else command_for_step(plan, step_id))
                publish_registered_work(command.work_kind, command.payload,
                    job_id=command.job_id, parent_job_id=parent_job(plan, step),
                    headers={"plan_id": plan.plan_id, "step_id": step_id,
                        "executor_deadline": command.deadline_at})
                delivery.published_at = timezone.now()
                delivery.publish_error = ""
                delivery.save(update_fields=["published_at", "publish_error"])
                if step_id == plan.steps[0].step_id:
                    row.published_at = delivery.published_at
                row.publish_error = ""
                row.save(update_fields=["published_at", "publish_error", "updated_at"])
        return row
    except Exception as exc:
        delivery_model().objects.filter(plan_id=plan_id, step_id=step_id).update(publish_error=str(exc)[:1000])
        Model.objects.filter(pk=plan_id).update(publish_error=str(exc)[:1000])
        raise


def _publish_ready(plan_id, *, replay=False):
    row = submission_model().objects.get(pk=plan_id)
    plan = load_submission(row)
    validate_submission(row, plan)
    reports = live_reports(row, plan)
    eligible = ready_steps(plan, reports)
    # Explicit manual redelivery covers sent jobs with no terminal report too.
    if replay:
        ready = {r.step_id for r in reports if r.status == "succeeded" and r.artifact_refs}
        succeeded = {r.step_id for r in reports if r.status == "succeeded"}
        eligible = [step for step in plan.steps if step.step_id not in succeeded and set(step.depends_on) <= ready]
    for step in eligible:
        publish_step(plan_id, step.step_id, replay=replay)
    row.refresh_from_db()
    return row


def publish_submission(plan_id, *, replay=False):
    try:
        return _publish_ready(plan_id, replay=replay)
    except Exception as exc:
        submission_model().objects.filter(pk=plan_id).update(publish_error=str(exc)[:1000])
        raise


class PlanDelivery:
    def _approved_delivery(self, kind, envelope, payload, *, row=None):
        """Check stored command identity, not permission to execute it now.

        Internal delivery callers must still use the domain report adapter to
        verify the durable receipt. This is not a public report authorization.
        """
        Model = submission_model()
        try:
            row = row or Model.objects.get(plan_id=envelope["plan_id"])
            if row.plan_id != envelope["plan_id"]:
                raise WorkContractError("Reported plan identity changed")
            if row.control.get("publication_mode") == "preparation":
                raise WorkContractError("Preparation plans cannot authorize execution")
            plan = load_submission(row)
            step = next(s for s in plan.steps if s.step_id == envelope["step_id"])
            command = stored_command(row, plan, step)
            if (kind != command.work_kind or envelope.get("job_id") != command.job_id
                    or payload != command.payload or envelope.get("deadline_at") != command.deadline_at
                    or envelope.get("parent_job_id", parent_job(plan, step)) != parent_job(plan, step)):
                raise PlanValidationError("Command is outside the stored approved step")
            delivery_model().objects.get(plan=row, step_id=step.step_id, job_id=command.job_id)
            if step.depends_on and envelope.get("parent_job_id") != parent_job(plan, step):
                raise WorkContractError("Child command is missing its approved lineage")
        except (Model.DoesNotExist, delivery_model().DoesNotExist, KeyError, ValueError, StopIteration) as exc:
            raise WorkContractError(str(exc)) from exc
        return row

    def authorize_report(self, kind, envelope, payload):
        """Internal receipt lookup/report linkage; grants no execution rights."""
        return self._approved_delivery(kind, envelope, payload)

    def authorize(self, kind, envelope, payload):
        row = self._approved_delivery(kind, envelope, payload)
        plan = load_submission(row)
        step = next(s for s in plan.steps if s.step_id == envelope["step_id"])
        validate_submission(row, plan)
        ready = {r.step_id for r in live_reports(row, plan) if r.status == "succeeded" and r.artifact_refs}
        if set(step.depends_on) - ready:
            raise WorkContractError("Step dependencies are not ready")
        return row

    def record_response(self, kind, envelope, payload, response):
        row = self._approved_delivery(kind, envelope, payload)
        validate_followups(response.get("followups", []))
        with transaction.atomic():
            current = submission_model().objects.select_for_update().get(pk=row.pk)
            plan = load_submission(current)
            self._approved_delivery(kind, envelope, payload, row=current)
            report = get_plan_adapter(kind).report(plan, current, response)
            if (report.plan_id != plan.plan_id or report.step_id != envelope["step_id"]
                    or report.job_id != envelope["job_id"]):
                raise WorkContractError("Reported result does not belong to the approved step")
            previous = current.reports.get(report.step_id)
            if previous and previous.get("status") == "succeeded" and report.status != "succeeded":
                raise WorkContractError("A successful step cannot regress")
            current.reports = {**current.reports, report.step_id: asdict(report)}
            current.proposals = {**current.proposals, report.step_id: response.get("followups", [])}
            reports = live_reports(current, plan)
            current.control = {**current.control, "outcome": completion_candidate(plan, reports),
                "decisions": {sid: decide_proposals(plan, proposals, from_step=sid, reports=reports,
                                                   commands=approved_commands(current, plan))
                    for sid, proposals in current.proposals.items()}}
            current.save(update_fields=["reports", "proposals", "control", "updated_at"])
        # Durable report commits before children can be consumed. Errors bubble
        # to the existing Celery retry path, which replays the parent's receipt.
        if report.status == "succeeded":
            transaction.on_commit(lambda: publish_submission(row.pk))
        return report


DELIVERY = PlanDelivery()


def inspect_submission(plan_id, *, actor, workspace):
    row = submission_model().objects.get(pk=plan_id, workspace_id=workspace.pk, created_by_id=actor.pk)
    plan = load_submission(row)
    validate_submission(row, plan)
    reports = live_reports(row, plan)
    return {"plan_id": row.plan_id, "outcome": completion_candidate(plan, reports),
        "decisions": {sid: decide_proposals(plan, proposals, from_step=sid, reports=reports,
                                           commands=approved_commands(row, plan))
                      for sid, proposals in row.proposals.items()}}


def authorize_if_planned(envelope):
    if (envelope.get("plan_id") or envelope.get("step_id")
            or submission_model().objects.filter(job_id=envelope.get("job_id", "")).exists()
            or delivery_model().objects.filter(job_id=envelope.get("job_id", "")).exists()):
        DELIVERY.authorize(envelope.get("work_kind"), envelope, envelope["payload"])
