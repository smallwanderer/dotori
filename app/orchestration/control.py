"""Bounded decisions for existing approved steps, not plan expansion."""
from orchestration.contracts import validate_followups
from orchestration.plans import command_for_step, requested_outputs_ready


def decide_proposals(plan, proposals, *, from_step=None, reports=(), commands=None):
    validate_followups(proposals)
    approved = ({command.job_id: command for command in commands} if commands is not None else
        {command_for_step(plan, step.step_id).job_id: command_for_step(plan, step.step_id)
         for step in plan.steps if step.input_binding is None})
    decisions = []
    ready = {r.step_id for r in reports if r.status == "succeeded" and r.artifact_refs}
    reported = {r.step_id for r in reports}
    for proposal in proposals:
        command = approved.get(proposal["job_id"])
        step = next((s for s in plan.steps if command and s.step_id == command.step_id), None)
        parent = (command_for_step(plan, step.depends_on[0]).job_id
                  if step and step.depends_on else plan.plan_id)
        same = (command is not None and proposal["work_kind"] == command.work_kind
                and proposal["payload"] == command.payload
                and proposal.get("parent_job_id", parent) == parent)
        decision, reason = "rejected", "OUTSIDE_APPROVED_PLAN"
        if same:
            if from_step is None or step.step_id == from_step or step.step_id in reported:
                decision, reason = "duplicate", "ALREADY_APPROVED_COMMAND"
            elif from_step not in step.depends_on:
                reason = "INVALID_STEP_LINEAGE"
            elif set(step.depends_on) - ready:
                decision, reason = "blocked", "DEPENDENCIES_NOT_READY"
            else:
                decision, reason = "accepted", "APPROVED_STEP_READY"
        decisions.append({"job_id": proposal["job_id"],
            "decision": decision, "reason": reason})
    return decisions


def completion_candidate(plan, reports):
    reports = tuple(reports)
    if requested_outputs_ready(plan, reports):
        return "completed"
    if any(report.status == "failed" for report in reports):
        return "failed"
    return "pending"
