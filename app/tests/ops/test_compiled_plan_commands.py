"""Real common journal/transport boundaries; synthetic owning-domain evidence."""
from contextlib import nullcontext
from dataclasses import replace
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.db import transaction

from orchestration import submissions, command_inputs
from orchestration.artifact_bindings import ArtifactBinding, ResolvedArtifact
from orchestration.contracts import WorkContractError
from orchestration.plans import (ExecutionPlan, ProcessingRequest, SourceReference,
    PlanStep, StepReport, command_for_step, plan_from_record, validate_plan_bindings)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def pipeline(monkeypatch):
    from django.contrib.auth import get_user_model
    from workspaces.models import Workspace
    actor = get_user_model().objects.create_user(email="compiled@example.com", password="test")
    workspace = Workspace.objects.create(name="compiled", created_by=actor)
    source = SourceReference("source", "blob", "source.schema", "checksum")
    artifact = ResolvedArtifact("artifact:1", "extract.schema", "parse-1", "source", "blob",
        "checksum", "policy", "ready", False, "old-creator")
    binding = ArtifactBinding("artifact", artifact.artifact_id, artifact.schema, artifact.revision)
    step = PlanStep("embed", "parse_document", "synthetic.function", artifact.schema,
        "output.schema", {"node_id": 1}, input_binding=binding)
    plan = ExecutionPlan("compiled-plan", 2,
        ProcessingRequest("compiled-request", source, ("output.schema",), time.time() + 100),
        (step,), "synthetic-generation")
    owner = Mock()
    owner.source_guard.side_effect = lambda *args: nullcontext()
    owner.validate.side_effect = lambda plan, row: validate_plan_bindings(plan)
    owner.resolve_input.return_value = artifact
    # Use an existing envelope shape; this fixture does not enable a new route.
    owner.compile_command.return_value = {"node_id": 23}
    def validate_payload(plan, row, step, payload, artifact):
        if payload != {"node_id": 23}:
            raise WorkContractError("manifest mismatch")
    owner.validate_compiled_command.side_effect = validate_payload
    monkeypatch.setattr(submissions, "get_plan_adapter", lambda _: owner)
    monkeypatch.setattr(command_inputs, "get_plan_adapter", lambda _: owner)
    with patch("orchestration.submissions._publish_after_commit"):
        row = submissions.submit_plan(plan, actor=actor, workspace=workspace)
    return SimpleNamespace(row=row, plan=plan, step=step, owner=owner, artifact=artifact,
                           actor=actor, workspace=workspace)


def test_concrete_payload_and_input_are_committed_before_transport(pipeline):
    def send(kind, payload, **kwargs):
        from orchestration_state.models import PlanSubmission
        stored = PlanSubmission.objects.get(pk=pipeline.row.pk)
        record = stored.control["compiled_commands"]["embed"]
        assert record["command"]["payload"] == payload == {"node_id": 23}
        assert record["input"]["producer_job_id"] == "old-creator"
    with patch("orchestration.submissions.publish_registered_work", side_effect=send) as publish:
        submissions.publish_submission(pipeline.row.pk)
    publish.assert_called_once()
    pipeline.owner.compile_command.assert_called_once()


def test_broker_failure_retains_frozen_command_and_replay_never_recompiles(pipeline):
    with patch("orchestration.submissions.publish_registered_work", side_effect=RuntimeError("broker")):
        with pytest.raises(RuntimeError):
            submissions.publish_submission(pipeline.row.pk)
    pipeline.row.refresh_from_db()
    frozen = pipeline.row.control["compiled_commands"]["embed"]
    assert pipeline.row.publish_error == "broker"
    pipeline.owner.compile_command.side_effect = AssertionError("Replay must not compile again")
    with patch("orchestration.submissions.publish_registered_work") as publish:
        submissions.publish_submission(pipeline.row.pk, replay=True)
    assert publish.call_args.kwargs["job_id"] == frozen["command"]["job_id"]
    assert publish.call_args.args[1] == frozen["command"]["payload"]


def test_compilation_failure_rolls_back_and_never_sends(pipeline):
    pipeline.owner.validate_compiled_command.side_effect = WorkContractError("manifest invalid")
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(WorkContractError):
            submissions.publish_submission(pipeline.row.pk)
    publish.assert_not_called()
    pipeline.row.refresh_from_db()
    assert "compiled_commands" not in pipeline.row.control


def step_pipeline(pipeline):
    # Step references pin concrete revisions only after the producer completes.
    binding = ArtifactBinding("step", "extract", "extract.schema")
    root = PlanStep("extract", "parse_document", "synthetic.extract", "source.schema",
                    "extract.schema", {"node_id": 1})
    consumer = replace(pipeline.step, input_binding=binding, depends_on=("extract",))
    plan = replace(pipeline.plan, steps=(root, consumer))
    from orchestration.plans import plan_record
    from orchestration_state.models import PlanCommandDelivery
    root_command = command_for_step(plan, "extract")
    pipeline.row.definition = plan_record(plan)
    pipeline.row.job_id = root_command.job_id
    pipeline.row.reports = {"extract": {"plan_id": plan.plan_id, "step_id": "extract",
        "job_id": root_command.job_id, "status": "succeeded", "artifact_refs": ["artifact:1"]}}
    pipeline.row.save(update_fields=["definition", "job_id", "reports"])
    PlanCommandDelivery.objects.create(plan=pipeline.row, step_id="extract", job_id=root_command.job_id)
    pipeline.owner.report.return_value = StepReport(plan.plan_id, "extract", root_command.job_id,
                                                    "succeeded", ("artifact:1",))
    pipeline.owner.resolve_input.return_value = replace(pipeline.artifact, producer_job_id=root_command.job_id)
    return plan, root_command


def test_stale_step_artifact_cannot_rebind_same_job(pipeline):
    step_pipeline(pipeline)
    command_inputs.prepare_step_command(pipeline.row.pk, "embed")
    pipeline.owner.resolve_input.return_value = replace(pipeline.owner.resolve_input.return_value, revision="parse-2")
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(WorkContractError, match="changed"):
            submissions.publish_step(pipeline.row.pk, "embed")
    publish.assert_not_called()
    pipeline.row.refresh_from_db()
    assert pipeline.row.control["compiled_commands"]["embed"]["input"]["revision"] == "parse-1"


def test_executor_authorizes_saved_payload_and_rejects_unresolved_template(pipeline):
    command = command_inputs.prepare_step_command(pipeline.row.pk, "embed")
    envelope = {"plan_id": command.plan_id, "step_id": command.step_id,
        "job_id": command.job_id, "deadline_at": command.deadline_at}
    assert submissions.DELIVERY.authorize(command.work_kind, envelope, command.payload).pk == pipeline.row.pk
    with pytest.raises(WorkContractError):
        submissions.DELIVERY.authorize(command.work_kind, envelope, pipeline.step.payload)


def test_no_publication_inside_uncommitted_outer_transaction(pipeline):
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(WorkContractError, match="committed"), transaction.atomic():
            submissions.publish_submission(pipeline.row.pk)
    publish.assert_not_called()


def test_request_id_cannot_change_input_binding(pipeline):
    changed = replace(pipeline.plan, steps=(replace(pipeline.step,
        input_binding=replace(pipeline.step.input_binding, revision="new")),))
    with pytest.raises(ValueError, match="another plan"):
        submissions.submit_plan(changed, actor=pipeline.actor, workspace=pipeline.workspace)


def test_missing_domain_compiler_rejects_before_send(pipeline):
    pipeline.owner.compile_command = None
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(WorkContractError, match="compile"):
            submissions.publish_submission(pipeline.row.pk)
    publish.assert_not_called()


@pytest.mark.parametrize("change", ["not_ready", "wrong_producer", "outside_report"])
def test_step_binding_requires_ready_report_exact_producer_and_artifact(pipeline, change):
    plan, root_command = step_pipeline(pipeline)
    if change == "not_ready":
        pipeline.row.reports = {}
        pipeline.row.save(update_fields=["reports"])
    elif change == "wrong_producer":
        pipeline.owner.resolve_input.return_value = replace(pipeline.artifact, producer_job_id="other")
    else:
        pipeline.owner.report.return_value = StepReport(plan.plan_id, "extract", root_command.job_id,
                                                        "succeeded", ("other-artifact",))
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(ValueError):
            submissions.publish_step(pipeline.row.pk, "embed")
    publish.assert_not_called()
    pipeline.owner.compile_command.assert_not_called()


def test_command_journal_failure_never_sends(pipeline):
    from orchestration_state.models import PlanSubmission
    with patch.object(PlanSubmission, "save", side_effect=RuntimeError("journal failed")), \
         patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(RuntimeError, match="journal failed"):
            submissions.publish_submission(pipeline.row.pk)
    publish.assert_not_called()
    pipeline.row.refresh_from_db()
    assert "compiled_commands" not in pipeline.row.control


def test_callback_mutation_cannot_change_frozen_payload(pipeline):
    command_inputs.prepare_step_command(pipeline.row.pk, "embed")
    pipeline.owner.validate_compiled_command.side_effect = lambda plan, row, step, payload, artifact: payload.update(node_id=99)
    with patch("orchestration.submissions.publish_registered_work") as publish:
        submissions.publish_submission(pipeline.row.pk)
    assert publish.call_args.args[1] == {"node_id": 23}


def test_uncompiled_template_is_not_an_executable_approval(pipeline):
    from orchestration.control import decide_proposals
    template = command_for_step(pipeline.plan, "embed")
    proposal = {"job_id": template.job_id, "work_kind": template.work_kind, "payload": template.payload}
    assert decide_proposals(pipeline.plan, [proposal])[0]["decision"] == "rejected"
    with pytest.raises(WorkContractError, match="compiled"):
        submissions.DELIVERY.authorize(template.work_kind,
            {"plan_id": template.plan_id, "step_id": template.step_id,
             "job_id": template.job_id, "deadline_at": template.deadline_at}, template.payload)


@pytest.mark.parametrize("change", ["job", "payload", "input"])
def test_tampered_frozen_command_cannot_be_published(pipeline, change):
    command_inputs.prepare_step_command(pipeline.row.pk, "embed")
    pipeline.row.refresh_from_db()
    record = pipeline.row.control["compiled_commands"]["embed"]
    if change == "job":
        record["command"]["job_id"] = "wrong"
    elif change == "payload":
        record["command"]["payload"]["node_id"] = 99
    else:
        record["input"]["revision"] = "changed"
    pipeline.row.save(update_fields=["control"])
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(WorkContractError):
            submissions.publish_submission(pipeline.row.pk)
    publish.assert_not_called()
