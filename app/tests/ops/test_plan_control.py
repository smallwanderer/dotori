from orchestration.control import decide_proposals, completion_candidate
from orchestration.plans import ExecutionPlan, ProcessingRequest, SourceReference, PlanStep, StepReport, command_for_step


def plan():
    return ExecutionPlan("p", 1, ProcessingRequest("r", SourceReference("s", "v", "input"), ("output",), 100),
        (PlanStep("step", "parse_document", "document.extract", "input", "output", {"node_id": 1}),), "generation")


def test_capability_availability_does_not_approve_an_extra_step():
    decisions = decide_proposals(plan(), [{"job_id": "child", "work_kind": "plan_document_embedding",
        "payload": {"node_id": 1}}])
    assert decisions == [{"job_id": "child", "decision": "rejected", "reason": "OUTSIDE_APPROVED_PLAN"}]


def test_existing_command_is_duplicate_not_a_new_publication():
    p = plan()
    command = command_for_step(p, "step")
    proposal = {"job_id": command.job_id, "work_kind": command.work_kind, "payload": command.payload}
    assert decide_proposals(p, [proposal])[0]["decision"] == "duplicate"
    proposal["parent_job_id"] = "another-plan"
    assert decide_proposals(p, [proposal])[0]["decision"] == "rejected"
    del proposal["parent_job_id"]
    proposal["payload"] = {"node_id": 2}
    assert decide_proposals(p, [proposal])[0]["decision"] == "rejected"


def test_completion_needs_a_validated_success_artifact():
    p = plan()
    command = command_for_step(p, "step")
    assert completion_candidate(p, []) == "pending"
    assert completion_candidate(p, [StepReport(p.plan_id, "step", command.job_id, "running", ())]) == "pending"
    assert completion_candidate(p, [StepReport(p.plan_id, "step", command.job_id, "failed", ())]) == "failed"
    assert completion_candidate(p, [StepReport(p.plan_id, "step", command.job_id, "succeeded", ("ref",))]) == "completed"
