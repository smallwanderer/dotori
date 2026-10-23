"""Durable intake, publication replay and plan-aware queue/HTTP boundaries."""
from dataclasses import replace
from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.db import transaction
from django.test import RequestFactory

from capabilities.deployment import clear_deployment_snapshot_cache
from documents.models import DocumentParseResult, DocumentChunk
from orchestration_state.models import PlanSubmission
from documents.services.plan_delivery import submit_document_plan, publish_submission, ADAPTER
from installation.deployment import build_module_deployment_plan, write_deployment_plan
from orchestration.contracts import WorkContractError
from orchestration.plans import plan_from_record, command_for_step, plan_record
from orchestration.celery_adapter import dispatch_or_retry

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def deployment(tmp_path, monkeypatch):
    path = write_deployment_plan(build_module_deployment_plan(["documents"]), repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    clear_deployment_snapshot_cache()
    yield
    clear_deployment_snapshot_cache()


@pytest.fixture
def doc():
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from workspaces.models import Workspace, WorkspaceMembership
    from files.models import Node, FileBlob
    actor = get_user_model().objects.create_user(email="submission@example.com", password="test")
    workspace = Workspace.objects.create(name="submission", created_by=actor)
    membership = WorkspaceMembership.objects.create(user=actor, workspace=workspace, role="admin")
    source = Node.objects.create(owner=actor, workspace=workspace, name="source.txt", ext=".txt", node_type="file")
    with patch("documents.signals._publish_uploaded_blob"):
        FileBlob.objects.create(node=source, original_name="source.txt", mime_type="text/plain", size=4,
            sha256="checksum", status="ready", file=SimpleUploadedFile("source.txt", b"text"))
    return SimpleNamespace(actor=actor, workspace=workspace, membership=membership, source=source)


def submit(doc, request_id="request", **kwargs):
    return submit_document_plan(actor=doc.actor, workspace=doc.workspace, membership=doc.membership,
                               source_uid=doc.source.uid, request_id=request_id, **kwargs)


def envelope(row):
    plan = plan_from_record(row.definition)
    command = command_for_step(plan, "initial-extract")
    return dict(plan_id=row.plan_id, step_id=command.step_id, job_id=command.job_id,
        work_kind=command.work_kind, envelope_version=1, payload=command.payload,
        deadline_at=command.deadline_at)


def test_submission_is_durable_before_publish_and_idempotent(doc):
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with transaction.atomic():
            row = submit(doc)
            assert PlanSubmission.objects.filter(pk=row.pk).exists()
            publish.assert_not_called()
        publish.assert_called_once()
        again = submit(doc)
        assert again.plan_id == row.plan_id
        assert publish.call_count == 1
    row.refresh_from_db()
    assert row.published_at is not None
    headers = publish.call_args.kwargs["headers"]
    assert headers["plan_id"] == row.plan_id
    assert publish.call_args.kwargs["job_id"] == row.job_id


def test_rollback_does_not_leave_plan_or_publish(doc):
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(RuntimeError), transaction.atomic():
            submit(doc)
            raise RuntimeError("rollback")
        assert not PlanSubmission.objects.exists()
        publish.assert_not_called()


def test_publish_failure_survives_reload_and_replays_same_job(doc):
    with patch("orchestration.submissions.publish_registered_work", side_effect=RuntimeError("broker outage")):
        row = submit(doc)
    row = PlanSubmission.objects.get(pk=row.pk)
    assert row.published_at is None
    assert "broker outage" in row.publish_error
    original = row.job_id
    with patch("orchestration.submissions.publish_registered_work") as publish:
        publish_submission(row.pk)
        assert publish.call_args.kwargs["job_id"] == original
        publish_submission(row.pk)
        assert publish.call_count == 1
        publish_submission(row.pk, replay=True)
        assert publish.call_count == 2
        assert publish.call_args.kwargs["job_id"] == original


def test_send_succeeds_but_journal_save_fails_replay_keeps_identity(doc):
    with patch("orchestration.submissions._publish_after_commit"):
        row = submit(doc)
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with patch.object(PlanSubmission, "save", side_effect=RuntimeError("write failed")):
            with pytest.raises(RuntimeError, match="write failed"):
                publish_submission(row.pk)
        row.refresh_from_db()
        assert row.published_at is None
        assert "write failed" in row.publish_error
        publish_submission(row.pk, replay=True)
        assert publish.call_count == 2
        assert {call.kwargs["job_id"] for call in publish.call_args_list} == {row.job_id}


def test_request_id_cannot_change_deadline(doc):
    import time
    deadline = time.time() + 1000
    with patch("orchestration.submissions.publish_registered_work"):
        submit(doc, deadline_at=deadline)
        with pytest.raises(ValueError, match="deadline"):
            submit(doc, deadline_at=deadline + 1)


@pytest.mark.parametrize("change", ["permission", "source", "deadline", "deployment"])
def test_pending_publish_revalidates_current_conditions(doc, change):
    with patch("orchestration.submissions._publish_after_commit"):
        row = submit(doc)
    if change == "permission":
        type(doc.membership).objects.filter(pk=doc.membership.pk).update(status="pending")
    elif change == "source":
        from files.models import FileBlob
        FileBlob.objects.filter(node=doc.source).update(sha256="new")
    else:
        plan = plan_from_record(row.definition)
        plan = replace(plan, request=replace(plan.request, deadline_at=1)) if change == "deadline" else replace(plan, deployment_generation="old")
        row.definition = plan_record(plan)
        row.save(update_fields=["definition"])
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(WorkContractError):
            publish_submission(row.pk)
    publish.assert_not_called()
    row.refresh_from_db()
    assert row.publish_error and row.published_at is None


def test_executor_rejects_stripped_plan_context_before_claim(doc, settings, monkeypatch):
    from orchestration import executor_views
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "parser")
    with patch("orchestration.submissions._publish_after_commit"):
        row = submit(doc)
    body = envelope(row)
    del body["plan_id"]
    del body["step_id"]
    request = RequestFactory().post("/", json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION="Bearer token")
    with patch("orchestration.receipts.claim_execution") as claim:
        response = executor_views.execute_job(request)
    assert response.status_code == 422
    claim.assert_not_called()


def test_plan_queue_response_records_artifact_and_proposal_without_publication(doc):
    with patch("orchestration.submissions._publish_after_commit"):
        row = submit(doc)
    body = envelope(row)
    result = DocumentParseResult.objects.create(node=doc.source, status="completed", parser_name="test",
        metadata={"source_blob_uuid": str(doc.source.blob.uuid), "source_checksum": "checksum"},
        performance_metrics={"job_id": row.job_id})
    DocumentChunk.objects.create(parse_result=result, chunk_index=0, text="text")
    followup = {"job_id": "unapproved-child", "work_kind": "plan_document_embedding", "payload": {"node_id": doc.source.id}}
    response = {"job_id": row.job_id, "work_kind": "parse_document", "status": "succeeded",
                "result": {"status": "success"}, "followups": [followup]}
    task = SimpleNamespace(request=SimpleNamespace(retries=0, headers={
        "plan_id": row.plan_id, "step_id": "initial-extract", "executor_deadline": body["deadline_at"]}))
    publish = Mock()
    assert dispatch_or_retry(task, "parse_document", envelope={"job_id": row.job_id},
        payload=body["payload"], dispatch=Mock(return_value=response), publish_followups=publish)["status"] == "success"
    publish.assert_not_called()
    row.refresh_from_db()
    assert row.reports["initial-extract"]["artifact_refs"] == [f"document-extract:{result.id}"]
    assert row.proposals["initial-extract"] == [followup]
    assert row.control["outcome"] == "completed"
    assert row.control["decisions"]["initial-extract"][0]["decision"] == "rejected"
    from orchestration.submissions import inspect_submission
    assert inspect_submission(row.pk, actor=doc.actor, workspace=doc.workspace)["outcome"] == "completed"
    result.delete()
    with pytest.raises(WorkContractError):
        inspect_submission(row.pk, actor=doc.actor, workspace=doc.workspace)


def test_invalid_plan_command_never_dispatches_or_mutates_domain(doc):
    with patch("orchestration.submissions._publish_after_commit"):
        row = submit(doc)
    body = envelope(row)
    task = SimpleNamespace(request=SimpleNamespace(retries=0, headers={
        "plan_id": row.plan_id, "step_id": "initial-extract", "executor_deadline": body["deadline_at"]}))
    send, reject = Mock(), Mock()
    output = dispatch_or_retry(task, "parse_document", envelope={"job_id": "wrong"},
        payload=body["payload"], dispatch=send, on_rejected=reject)
    assert output["error_code"] == "INVALID_PLAN_COMMAND"
    send.assert_not_called()
    reject.assert_not_called()


def test_command_is_real_producer_and_replay_entry_point(doc):
    from django.core.management import call_command
    with patch("orchestration.submissions.publish_registered_work") as publish:
        call_command("submit_document_plan", source_uid=str(doc.source.uid), workspace_uid=str(doc.workspace.uid),
                     actor_id=doc.actor.id, request_id="operator", stdout=StringIO())
        row = PlanSubmission.objects.get(request_id="operator")
        call_command("submit_document_plan", replay_plan_id=row.pk, stdout=StringIO())
    assert publish.call_count == 2


def test_two_domain_adapters_share_the_common_journal(doc, monkeypatch):
    from contextlib import nullcontext
    import time
    from orchestration import submissions
    from orchestration.plans import ExecutionPlan, ProcessingRequest, SourceReference, PlanStep, StepReport

    adapters = {kind: Mock() for kind in ("test.image", "test.audio")}
    monkeypatch.setattr(submissions, "get_plan_adapter", lambda kind: adapters[kind])
    with patch("orchestration.submissions.publish_registered_work") as publish:
        rows = []
        for kind, adapter in adapters.items():
            source = SourceReference(kind + ":asset", "revision", kind + ".source")
            plan = ExecutionPlan(kind + ":plan", 1,
                ProcessingRequest(kind + ":request", source, (kind + ".result",), time.time() + 100),
                (PlanStep("prepare", kind, kind + ".function", source.schema, kind + ".result", {"asset": source.source_id}),),
                "test-generation")
            adapter.source_guard.side_effect = lambda *args: nullcontext()
            row = submissions.submit_plan(plan, actor=doc.actor, workspace=doc.workspace)
            rows.append(row)
            command = command_for_step(plan, "prepare")
            adapter.report.return_value = StepReport(plan.plan_id, "prepare", command.job_id, "succeeded", (kind + ":result",))
            submissions.DELIVERY.record_response(kind,
                {"plan_id": plan.plan_id, "step_id": "prepare", "job_id": command.job_id, "deadline_at": command.deadline_at},
                command.payload, {"followups": []})
            row.refresh_from_db()
            assert row.reports["prepare"]["artifact_refs"] == [kind + ":result"]
            adapter.validate.assert_called()
        assert publish.call_count == 2
        assert {row.source_uid for row in rows} == {"test.image:asset", "test.audio:asset"}
    assert PlanSubmission._meta.app_label == "orchestration_state"
    assert "source" not in {field.name for field in PlanSubmission._meta.fields}


def test_transfer_migration_preserves_legacy_record_and_job(doc):
    """Execute the source-reference migration on a temporary legacy table."""
    from importlib import import_module
    from django.db import connection
    from django.db.migrations.loader import MigrationLoader

    loader = MigrationLoader(None)
    state = loader.project_state([("documents", "0003_rename_plan_submission")])
    state.models["documents", "plansubmission"].options["db_table"] = "test_plan_transfer"
    state.models["documents", "plansubmission"].options["constraints"] = []
    Legacy = state.apps.get_model("documents", "PlanSubmission")
    with connection.schema_editor() as editor:
        editor.create_model(Legacy)
    try:
        row = Legacy.objects.create(plan_id="retained-plan", request_id="retained-request", job_id="retained-job",
            workspace_id=doc.workspace.pk, created_by_id=doc.actor.pk, source_id=doc.source.pk,
            definition={"retained": True}, reports={"result": "ref"}, proposals={"step": []})
        migration = import_module("documents.migrations.0004_plan_source_reference").Migration("0004", "documents")
        for operation in migration.operations:
            next_state = state.clone()
            operation.state_forwards("documents", next_state)
            with connection.schema_editor() as editor:
                operation.database_forwards("documents", editor, state, next_state)
            state = next_state
        Transferred = state.apps.get_model("documents", "PlanSubmission")
        retained = Transferred.objects.get(pk=row.pk)
        assert retained.source_uid == str(doc.source.uid)
        assert retained.job_id == "retained-job"
        assert retained.definition == {"retained": True}
        assert retained.reports == {"result": "ref"}
        assert retained.proposals == {"step": []}
        common_state = loader.project_state([("orchestration_state", "0001_initial")])
        common_state.models["orchestration_state", "plansubmission"].options["db_table"] = "test_plan_transfer"
        Common = common_state.apps.get_model("orchestration_state", "PlanSubmission")
        assert Common.objects.get(pk=row.pk).job_id == "retained-job"
    finally:
        with connection.schema_editor() as editor:
            editor.delete_model(state.apps.get_model("documents", "PlanSubmission"))


def test_source_deletion_keeps_plan_but_replay_is_rejected(doc):
    with patch("orchestration.submissions._publish_after_commit"):
        row = submit(doc)
    doc.source.delete()
    assert PlanSubmission.objects.filter(pk=row.pk).exists()
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(WorkContractError, match="source"):
            publish_submission(row.pk, replay=True)
    publish.assert_not_called()


@pytest.fixture
def static_pipeline(doc, monkeypatch):
    """Delivery mechanics only; admission/domain validity are isolated here."""
    from contextlib import nullcontext
    import time
    from orchestration import submissions
    from orchestration.plans import ExecutionPlan, ProcessingRequest, SourceReference, PlanStep, StepReport
    from orchestration_state.models import PlanCommandDelivery
    source = SourceReference("test-asset", "revision", "test.source")
    plan = ExecutionPlan("pipeline-plan", 1,
        ProcessingRequest("pipeline-request", source, ("left.result", "right.result"), time.time() + 100),
        (PlanStep("root", "parse_document", "test.root", source.schema, "middle", {"node_id": 1}),
         PlanStep("left", "parse_document", "test.left", "middle", "left.result", {"node_id": 2}, ("root",)),
         PlanStep("right", "parse_document", "test.right", "middle", "right.result", {"node_id": 3}, ("root",))),
        "test-generation")
    adapter = Mock()
    adapter.source_guard.side_effect = lambda *args: nullcontext()
    def report(p, row, response):
        step = next(s for s in p.steps if command_for_step(p, s.step_id).job_id == response["job_id"])
        return StepReport(p.plan_id, step.step_id, response["job_id"], response["status"],
            ("artifact:" + step.step_id,) if response["status"] == "succeeded" else ())
    adapter.report.side_effect = report
    monkeypatch.setattr(submissions, "get_plan_adapter", lambda kind: adapter)
    def body(step_id, status="succeeded"):
        from orchestration.submissions import parent_job
        step = next(s for s in plan.steps if s.step_id == step_id)
        command = command_for_step(plan, step_id)
        return {"plan_id": plan.plan_id, "step_id": step_id, "job_id": command.job_id,
            "work_kind": step.work_kind, "payload": command.payload, "deadline_at": command.deadline_at,
            "parent_job_id": parent_job(plan, step), "status": status, "followups": []}
    def record(step_id, *, followups=None, status="succeeded"):
        response = body(step_id, status)
        response["followups"] = followups or []
        return ADAPTER.record_response(response["work_kind"], response, response["payload"], response)
    return SimpleNamespace(plan=plan, adapter=adapter, body=body, record=record,
        submit=lambda: submissions.submit_plan(plan, actor=doc.actor, workspace=doc.workspace),
        deliveries=PlanCommandDelivery)


def test_static_plan_advances_only_after_dependencies_and_completes(doc, static_pipeline):
    from orchestration.submissions import inspect_submission
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = p.submit()
        assert publish.call_count == 1
        child = p.body("left")
        with pytest.raises(WorkContractError, match="dependencies"):
            ADAPTER.authorize(child["work_kind"], child, child["payload"])
        proposal = {key: child[key] for key in ("job_id", "work_kind", "payload", "parent_job_id")}
        p.record("root", followups=[proposal])
        assert publish.call_count == 3
        row.refresh_from_db()
        assert row.control["decisions"]["root"][0]["decision"] == "accepted"
        p.record("left")
        assert inspect_submission(row.pk, actor=doc.actor, workspace=doc.workspace)["outcome"] == "pending"
        p.record("right")
        assert inspect_submission(row.pk, actor=doc.actor, workspace=doc.workspace)["outcome"] == "completed"
        assert publish.call_count == 3


def test_partial_child_publish_keeps_report_and_successful_delivery(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work", side_effect=[None, None, RuntimeError("broker outage"), None]) as publish:
        row = p.submit()
        with pytest.raises(RuntimeError, match="broker outage"):
            p.record("root")
        row.refresh_from_db()
        assert row.reports["root"]["status"] == "succeeded"
        assert p.deliveries.objects.get(plan=row, step_id="left").published_at
        assert p.deliveries.objects.get(plan=row, step_id="right").published_at is None
        publish_submission(row.pk)
        ids = [call.kwargs["job_id"] for call in publish.call_args_list]
        assert ids.count(command_for_step(p.plan, "root").job_id) == 1
        assert ids.count(command_for_step(p.plan, "left").job_id) == 1
        assert ids.count(command_for_step(p.plan, "right").job_id) == 2


def test_parent_receipt_replay_recovers_child_without_republishing_parent(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work", side_effect=[None, RuntimeError("send failed"), None, None]) as publish:
        row = p.submit()
        with pytest.raises(RuntimeError):
            p.record("root")
        # This is the queue retry's report/link step, not parent domain execution.
        p.record("root")
        assert [call.kwargs["job_id"] for call in publish.call_args_list].count(row.job_id) == 1
        assert p.deliveries.objects.filter(plan=row, published_at__isnull=False).count() == 3


def test_child_publish_ambiguity_uses_identical_job_on_recovery(static_pipeline):
    p = static_pipeline
    original = p.deliveries.save
    failed = []
    def save(delivery, *args, **kwargs):
        if delivery.step_id == "left" and not failed:
            failed.append(True)
            raise RuntimeError("journal write failed")
        return original(delivery, *args, **kwargs)
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = p.submit()
        with patch.object(p.deliveries, "save", save), pytest.raises(RuntimeError):
            p.record("root")
        publish_submission(row.pk)
        ids = [call.kwargs["job_id"] for call in publish.call_args_list]
        assert ids.count(command_for_step(p.plan, "left").job_id) == 2
        assert ids.count(row.job_id) == 1


def test_recovery_rechecks_permissions_and_rejects_child_metadata_stripping(static_pipeline):
    from orchestration.submissions import authorize_if_planned
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = p.submit()
        p.record("root")
        child = p.body("left")
        del child["plan_id"]
        del child["step_id"]
        with pytest.raises(WorkContractError):
            authorize_if_planned(child)
        p.adapter.validate.side_effect = WorkContractError("access revoked")
        with pytest.raises(WorkContractError, match="access revoked"):
            publish_submission(row.pk, replay=True)
        assert publish.call_count == 3


def test_failure_report_survives_execution_permission_revocation(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = p.submit()
        p.adapter.validate.side_effect = WorkContractError("access revoked")
        body = p.body("root", "failed")
        with pytest.raises(WorkContractError, match="access revoked"):
            ADAPTER.authorize(body["work_kind"], body, body["payload"])
        p.adapter.validate.reset_mock()
        p.record("root", status="failed")
        p.adapter.validate.assert_not_called()
        row.refresh_from_db()
        assert row.reports["root"]["status"] == "failed"
        assert publish.call_count == 1


def test_success_report_commits_but_revoked_plan_cannot_publish_children(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = p.submit()
        p.adapter.validate.side_effect = WorkContractError("source changed")
        with pytest.raises(WorkContractError, match="source changed"):
            p.record("root")
        row.refresh_from_db()
        assert row.reports["root"]["status"] == "succeeded"
        assert publish.call_count == 1


@pytest.mark.parametrize("field,value", [("job_id", "forged"), ("parent_job_id", "forged"),
                                        ("deadline_at", 0), ("payload", {"node_id": 999})])
def test_revoked_plan_still_rejects_forged_report_identity(static_pipeline, field, value):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work"):
        row = p.submit()
    p.adapter.validate.side_effect = WorkContractError("access revoked")
    body = p.body("root", "failed")
    body[field] = value
    with pytest.raises(WorkContractError):
        ADAPTER.record_response(body["work_kind"], body, body["payload"], body)
    row.refresh_from_db()
    assert row.reports == {}
    p.adapter.report.assert_not_called()


def test_identity_match_does_not_bypass_durable_report_validation(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = p.submit()
        p.adapter.validate.side_effect = WorkContractError("access revoked")
        p.adapter.report.side_effect = WorkContractError("receipt attempt mismatch")
        with pytest.raises(WorkContractError, match="receipt attempt mismatch"):
            p.record("root", status="failed")
        row.refresh_from_db()
        assert row.reports == {}
        assert publish.call_count == 1


def test_report_rollback_never_publishes_children(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = p.submit()
        with pytest.raises(RuntimeError):
            with transaction.atomic():
                p.record("root")
                assert publish.call_count == 1
                raise RuntimeError("rollback")
        row.refresh_from_db()
        assert row.reports == {}
        assert publish.call_count == 1


def test_queue_retry_replays_parent_receipt_and_only_unsent_children(static_pipeline):
    from celery.exceptions import Retry
    p = static_pipeline
    parent = p.body("root")
    task = SimpleNamespace(request=SimpleNamespace(retries=0, headers={
        "plan_id": p.plan.plan_id, "step_id": "root", "executor_deadline": parent["deadline_at"]}),
        retry=Mock(side_effect=Retry))
    response = {"job_id": parent["job_id"], "work_kind": parent["work_kind"],
        "status": "succeeded", "result": {"status": "success"}, "followups": []}
    dispatch = Mock(return_value=response)
    with patch("orchestration.submissions.publish_registered_work", side_effect=[None, None, RuntimeError("broker down"), None]) as publish:
        row = p.submit()
        with pytest.raises(Retry):
            dispatch_or_retry(task, "parse_document", envelope={"job_id": row.job_id},
                payload=parent["payload"], dispatch=dispatch)
        assert dispatch_or_retry(task, "parse_document", envelope={"job_id": row.job_id},
            payload=parent["payload"], dispatch=dispatch)["status"] == "success"
        assert dispatch.call_count == 2
        assert {call.kwargs["envelope"]["job_id"] for call in dispatch.call_args_list} == {row.job_id}
        ids = [call.kwargs["job_id"] for call in publish.call_args_list]
        assert ids.count(command_for_step(p.plan, "left").job_id) == 1
        assert ids.count(command_for_step(p.plan, "right").job_id) == 2


def retry_task(p):
    from celery.exceptions import Retry
    parent = p.body("root")
    return SimpleNamespace(request=SimpleNamespace(retries=1, headers={
        "plan_id": p.plan.plan_id, "step_id": "root", "executor_deadline": parent["deadline_at"]}),
        retry=Mock(side_effect=Retry))


def test_terminal_failure_recovery_bypasses_execution_admission(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = p.submit()
        p.adapter.validate.side_effect = WorkContractError("access revoked")
        response = {**p.body("root", "failed"), "retryable": False}
        send, lookup = Mock(), Mock(return_value=response)
        with patch("orchestration.celery_adapter.require_capability") as admission:
            result = dispatch_or_retry(retry_task(p), "parse_document", envelope={"job_id": row.job_id},
                payload=response["payload"], dispatch=send, read_receipt=lookup)
        assert result["status"] == "failed"
        send.assert_not_called()
        admission.assert_not_called()
        row.refresh_from_db()
        assert row.reports["root"]["status"] == "failed"
        assert publish.call_count == 1


def test_completed_recovery_preserves_report_when_followup_admission_fails(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = p.submit()
        p.adapter.validate.side_effect = WorkContractError("source changed")
        response = p.body("root")
        send = Mock()
        result = dispatch_or_retry(retry_task(p), "parse_document", envelope={"job_id": row.job_id},
            payload=response["payload"], dispatch=send, read_receipt=Mock(return_value=response))
        assert result["error_code"] == "INVALID_PLAN_RESULT"
        send.assert_not_called()
        row.refresh_from_db()
        assert row.reports["root"]["status"] == "succeeded"
        assert publish.call_count == 1


@pytest.mark.parametrize("receipt", [{}, {"status": "running"}, {"status": "failed", "retryable": True}])
def test_nonterminal_recovery_still_requires_execution_admission(static_pipeline, receipt):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work"):
        row = p.submit()
    p.adapter.validate.side_effect = WorkContractError("access revoked")
    send = Mock()
    result = dispatch_or_retry(retry_task(p), "parse_document", envelope={"job_id": row.job_id},
        payload=p.body("root")["payload"], dispatch=send, read_receipt=Mock(return_value=receipt))
    assert result["error_code"] == "INVALID_PLAN_COMMAND"
    send.assert_not_called()
    row.refresh_from_db()
    assert row.reports == {}


def test_recovery_rejects_changed_command_before_receipt_lookup(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work"):
        row = p.submit()
    lookup, send = Mock(), Mock()
    result = dispatch_or_retry(retry_task(p), "parse_document", envelope={"job_id": row.job_id},
        payload={"node_id": 999}, dispatch=send, read_receipt=lookup)
    assert result["error_code"] == "INVALID_PLAN_COMMAND"
    lookup.assert_not_called()
    send.assert_not_called()


def test_success_receipt_recovery_publishes_only_approved_children(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = p.submit()
        response = p.body("root")
        send = Mock()
        result = dispatch_or_retry(retry_task(p), "parse_document", envelope={"job_id": row.job_id},
            payload=response["payload"], dispatch=send, read_receipt=Mock(return_value=response))
        assert result["status"] == "success"
        send.assert_not_called()
        assert publish.call_count == 3
        assert [call.kwargs["job_id"] for call in publish.call_args_list] == [
            command_for_step(p.plan, sid).job_id for sid in ("root", "left", "right")]


@pytest.mark.parametrize("field,value", [("job_id", "another-job"), ("work_kind", "another-kind")])
def test_receipt_lookup_identity_mismatch_is_rejected(static_pipeline, field, value):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work"):
        row = p.submit()
    response = {**p.body("root"), field: value}
    send = Mock()
    result = dispatch_or_retry(retry_task(p), "parse_document", envelope={"job_id": row.job_id},
        payload=p.body("root")["payload"], dispatch=send, read_receipt=Mock(return_value=response))
    assert result["error_code"] == "INVALID_PLAN_COMMAND"
    send.assert_not_called()
    row.refresh_from_db()
    assert row.reports == {}


def test_broker_redelivery_with_zero_celery_retries_recovers_terminal_report(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work"):
        row = p.submit()
    p.adapter.validate.side_effect = WorkContractError("access revoked")
    task = retry_task(p)
    task.request.retries = 0
    task.request.delivery_info = {"redelivered": True}
    response = {**p.body("root", "failed"), "retryable": False}
    send, lookup = Mock(), Mock(return_value=response)
    result = dispatch_or_retry(task, "parse_document", envelope={"job_id": row.job_id},
        payload=response["payload"], dispatch=send, read_receipt=lookup)
    assert result["status"] == "failed"
    send.assert_not_called()
    lookup.assert_called_once_with("parse_document", row.job_id)
    row.refresh_from_db()
    assert row.reports["root"]["status"] == "failed"


def test_terminal_failure_report_can_be_recovered_after_execution_deadline(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work"):
        row = p.submit()
    response = {**p.body("root", "failed"), "retryable": False}
    send = Mock()
    with patch("orchestration.celery_adapter.time.time", return_value=response["deadline_at"] + 1):
        result = dispatch_or_retry(retry_task(p), "parse_document", envelope={"job_id": row.job_id},
            payload=response["payload"], dispatch=send, read_receipt=Mock(return_value=response))
    assert result["status"] == "failed"
    send.assert_not_called()
    row.refresh_from_db()
    assert row.reports["root"]["status"] == "failed"


def test_recovery_lookup_failure_never_dispatches(static_pipeline):
    from celery.exceptions import Retry
    from orchestration.transport import ExecutorDispatchError
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work"):
        row = p.submit()
    send = Mock()
    with pytest.raises(Retry):
        dispatch_or_retry(retry_task(p), "parse_document", envelope={"job_id": row.job_id},
            payload=p.body("root")["payload"], dispatch=send,
            read_receipt=Mock(side_effect=ExecutorDispatchError("lookup unavailable")))
    send.assert_not_called()


def test_successful_report_cannot_be_overwritten_by_running_response(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work"):
        row = p.submit()
        p.record("root")
        with pytest.raises(WorkContractError, match="cannot regress"):
            p.record("root", status="running")
        row.refresh_from_db()
        assert row.reports["root"]["status"] == "succeeded"


def test_delivery_migration_backfills_stable_ids_and_initial_publication(doc, static_pipeline):
    from importlib import import_module
    from django.apps import apps
    from django.db import connection
    from django.utils import timezone
    p = static_pipeline
    stamp = timezone.now()
    row = PlanSubmission.objects.create(plan_id=p.plan.plan_id, request_id=p.plan.request.request_id,
        job_id=command_for_step(p.plan, "root").job_id, workspace=doc.workspace, created_by=doc.actor,
        source_uid=p.plan.request.source.source_id, definition=plan_record(p.plan),
        published_at=stamp, publish_error="old error")
    migration = import_module("orchestration_state.migrations.0003_plan_command_delivery")
    with connection.schema_editor() as editor:
        migration.backfill(apps, editor)
    root = p.deliveries.objects.get(plan=row, step_id="root")
    assert root.job_id == row.job_id and root.published_at == stamp
    assert root.publish_error == "old error"
    for sid in ("left", "right"):
        child = p.deliveries.objects.get(plan=row, step_id=sid)
        assert child.job_id == command_for_step(p.plan, sid).job_id
        assert child.published_at is None


def test_child_without_parent_lineage_is_rejected(static_pipeline):
    p = static_pipeline
    with patch("orchestration.submissions.publish_registered_work"):
        p.submit()
        p.record("root")
        child = p.body("left")
        del child["parent_job_id"]
        with pytest.raises(WorkContractError, match="lineage"):
            ADAPTER.authorize(child["work_kind"], child, child["payload"])
