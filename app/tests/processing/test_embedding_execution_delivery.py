"""Real plan/domain/HTTP handler integration; broker and model are isolated."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.test import RequestFactory

from documents.models import ExecutorJobReceipt, DocumentEmbeddingManifest, DocumentParseResult
from documents.processing.embedding_plan_adapter import submit_document_embedding_plan
from orchestration import executor_views
from orchestration.command_inputs import stored_command
from orchestration.plans import plan_from_record
from orchestration.submissions import DELIVERY, parent_job
from tests.processing.test_embedding_preparation import deployment, doc, parsed
from tests.processing.test_embedding_plan_journal import admitted
from tests.processing.test_embedding_manifests import approval
from tests.embedding.test_scoped_embedding_execution import scoped, embedding
from tests.embedding.test_embedding_persistence import isolated_runtime_cache

pytestmark = pytest.mark.django_db(transaction=True)


def submit(doc, **kwargs):
    return submit_document_embedding_plan(actor=doc.actor, workspace=doc.workspace,
        membership=doc.membership, source_uid=doc.node.uid, request_id="execute-real", **kwargs)


def command_envelope(row, step_id="embed"):
    plan = plan_from_record(row.definition)
    step = next(s for s in plan.steps if s.step_id == step_id)
    command = stored_command(row, plan, step)
    return {"plan_id": row.pk, "step_id": step_id, "job_id": command.job_id,
        "work_kind": command.work_kind, "payload": command.payload,
        "parent_job_id": parent_job(plan, step), "deadline_at": command.deadline_at,
        "envelope_version": 1}


def http(body):
    request = RequestFactory().post("/internal/v1/jobs/execute/", json.dumps(body),
        content_type="application/json", HTTP_AUTHORIZATION="Bearer token")
    response = executor_views.execute_job(request)
    return response, json.loads(response.content)


def test_reused_parse_execution_intake_http_and_report(doc, admitted, scoped, settings, monkeypatch):
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "embedding")
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = submit(doc, mode="refresh")
        assert publish.call_count == 1
        row.refresh_from_db()
        body = command_envelope(row)
        assert publish.call_args.kwargs["job_id"] == body["job_id"]
        assert publish.call_args.args == ("embed_document", body["payload"])
        with patch("documents.embedding.execution.run_embedding_with_admission",
                   side_effect=lambda _, texts: embedding(texts)) as inference:
            response, result = http(body)
            assert response.status_code == 200, result
            assert result["status"] == "succeeded", result
            assert inference.call_count == 2
            # Same HTTP delivery uses the receipt and never reinfers.
            assert http(body)[1]["status"] == "succeeded"
            assert inference.call_count == 2
        from documents.tasks import embed_document_scoped
        def post(url, *, json, **kwargs):
            response, result = http(json)
            return SimpleNamespace(status_code=response.status_code, headers={}, json=lambda: result)
        embed_document_scoped.push_request(retries=0, headers={"plan_id": row.pk,
            "step_id": "embed", "executor_deadline": body["deadline_at"]})
        try:
            with patch("orchestration.transport.requests.post", side_effect=post):
                output = embed_document_scoped.run(**body["payload"], job_id=body["job_id"],
                    parent_job_id=body["parent_job_id"], trace_id="trace", enqueued_at=None,
                    work_kind="embed_document", envelope_version=1)
            assert output["ready"]
        finally:
            embed_document_scoped.pop_request()
    row.refresh_from_db()
    assert row.control["outcome"] == "completed"
    assert row.reports["embed"]["status"] == "succeeded"
    receipt = ExecutorJobReceipt.objects.get(pk=body["job_id"])
    assert receipt.attempt == 1 and receipt.result["ready"]
    assert scoped.store.base_queryset().count() == 2


def test_execution_intake_does_not_promote_preparation(doc, admitted):
    from documents.processing.embedding_plan_adapter import build_document_embedding_plan
    from orchestration.submissions import submit_plan
    parsed(doc)
    plan = build_document_embedding_plan(actor=doc.actor, workspace=doc.workspace,
        membership=doc.membership, source_uid=doc.node.uid, request_id="execute-real")
    submit_plan(plan, actor=doc.actor, workspace=doc.workspace, preparation=True)
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(ValueError, match="publication mode"):
            submit(doc)
    publish.assert_not_called()


def test_parse_then_embedding_pins_exact_producer(doc, admitted, scoped, settings, monkeypatch):
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    DocumentParseResult.objects.filter(node=doc.node).delete()
    def parse_node(node_id, *, on_persisted, **kwargs):
        assert node_id == doc.node.pk
        result = parsed(doc)
        result.performance_metrics = {"job_id": kwargs["job_id"]}
        result.save(update_fields=["performance_metrics"])
        summary = {"status": "success", "node_id": node_id, "chunk_count": 2}
        on_persisted(summary)
        return summary
    with patch("orchestration.submissions.publish_registered_work") as publish:
        row = submit(doc)
        assert publish.call_args.args[0] == "parse_document"
        assert not DocumentEmbeddingManifest.objects.filter(plan=row).exists()
        body = command_envelope(row, "initial-extract")
        monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "parser")
        with patch("documents.processing.parsing.get_parser_for_execution", return_value=object()), \
             patch("documents.processing.parsing.execute_parse_node", side_effect=parse_node):
            response, result = http(body)
        assert response.status_code == 200, result
        DELIVERY.record_response("parse_document", body, body["payload"], result)
        row.refresh_from_db()
        assert publish.call_count == 2
        embedding_body = command_envelope(row)
        assert embedding_body["parent_job_id"] == body["job_id"]
        assert row.control["compiled_commands"]["embed"]["input"]["producer_job_id"] == body["job_id"]
        monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "embedding")
        with patch("documents.embedding.execution.run_embedding_with_admission",
                   side_effect=lambda _, texts: embedding(texts)):
            response, result = http(embedding_body)
        assert response.status_code == 200, result
        DELIVERY.record_response("embed_document", embedding_body, embedding_body["payload"], result)
        row.refresh_from_db()
        assert row.control["outcome"] == "completed"
        assert len(row.reports) == 2


def test_execution_intake_requires_installed_route(doc, admitted, monkeypatch):
    from dataclasses import replace
    parsed(doc)
    snapshot = replace(admitted, work_routes={k: v for k, v in admitted.work_routes.items()
                                              if k != "embed_document"})
    monkeypatch.setattr("capabilities.deployment.load_deployment_snapshot", lambda: snapshot)
    monkeypatch.setattr("orchestration.plans.load_deployment_snapshot", lambda: snapshot)
    with patch("orchestration.submissions.publish_registered_work") as publish:
        with pytest.raises(ValueError, match="registered"):
            submit(doc)
    publish.assert_not_called()


def test_source_change_during_http_execution_is_reported_without_vector_write(
        doc, admitted, scoped, settings, monkeypatch):
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "embedding")
    with patch("orchestration.submissions.publish_registered_work"):
        row = submit(doc, mode="refresh")
        row.refresh_from_db()
        body = command_envelope(row)
        def changed(_, texts):
            type(doc.blob).objects.filter(pk=doc.blob.pk).update(sha256="changed")
            return embedding(texts)
        with patch("documents.embedding.execution.run_embedding_with_admission", side_effect=changed):
            response, result = http(body)
        assert response.status_code == 200, result
        assert result["status"] == "failed" and result["retryable"] is False
        assert not scoped.store.base_queryset().exists()
        DELIVERY.record_response("embed_document", body, body["payload"], result)
        row.refresh_from_db()
        assert row.reports["embed"]["status"] == "failed"
        assert ExecutorJobReceipt.objects.get(pk=body["job_id"]).error == result["error"]


def test_transport_returns_terminal_domain_failure_for_report(settings):
    from orchestration.transport import dispatch_executor_job
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    body = {"ok": False, "job_id": "terminal", "work_kind": "embed_document",
        "status": "failed", "retryable": False,
        "error": {"code": "STALE_INPUT", "message": "source changed"}}
    response = SimpleNamespace(status_code=200, headers={}, json=lambda: body)
    with patch("orchestration.transport.requests.post", return_value=response):
        assert dispatch_executor_job("embed_document", envelope={"job_id": "terminal"}, payload={}) == body


def test_operator_command_uses_execution_intake(doc, admitted):
    from django.core.management import call_command
    from orchestration_state.models import PlanSubmission
    from io import StringIO
    parsed(doc)
    with patch("orchestration.submissions.publish_registered_work") as publish:
        call_command("submit_document_plan", embedding=True, mode="refresh",
            source_uid=str(doc.node.uid), workspace_uid=str(doc.workspace.uid), actor_id=doc.actor.pk,
            request_id="operator-embedding", stdout=StringIO())
        row = PlanSubmission.objects.get(request_id="operator-embedding")
        assert row.control.get("publication_mode") != "preparation"
        assert publish.call_count == 1 and publish.call_args.args[0] == "embed_document"
