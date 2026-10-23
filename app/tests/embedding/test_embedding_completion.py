from dataclasses import asdict, replace
from unittest.mock import patch

import pytest

from documents.embedding.completion import execute_scoped_receipted
from documents.processing.embedding_reports import embedding_step_report
from documents.processing.embedding_contract import build_embedding_result, TargetOutcome, parse_embedding_delivery
from documents.models import ExecutorJobReceipt, DocumentChunk
from documents.processing.recovery import scoped_recovery_blocked_node_ids
from orchestration.plans import plan_from_record, command_for_step
from orchestration.receipts import receipt_payload, ExecutionOwnershipLost
from orchestration.contracts import WorkContractError
from tests.processing.test_embedding_preparation import deployment, doc, parsed
from tests.processing.test_embedding_manifests import approval
from tests.embedding.test_scoped_embedding_execution import scoped, executing, embedding

pytestmark = pytest.mark.django_db


def journal(manifest, envelope):
    row = manifest.plan
    plan = plan_from_record(row.definition)
    compiled = replace(command_for_step(plan, "embed"), payload=envelope["payload"])
    # Internal adapter fixture, not active admission/publication authorization.
    row.control = {**row.control, "publication_mode": "preparation",
        "compiled_commands": {"embed": {"command": asdict(compiled)}}}
    row.save(update_fields=["control"])
    return row, plan


def test_historical_report_survives_input_change_without_vector_queries(scoped):
    envelope, manifest = scoped.configure()
    row, plan = journal(manifest, envelope)
    with executing(envelope), patch("documents.embedding.execution.run_embedding_with_admission",
            side_effect=lambda _, texts: embedding(texts)):
        receipt = execute_scoped_receipted(envelope)
    assert receipt.status == "succeeded"
    with patch("documents.models.DocumentChunk.objects.filter", side_effect=AssertionError("Report must not query chunks")):
        report = embedding_step_report(plan, row, receipt_payload(receipt))
        assert report.status == "succeeded"
        assert report.artifact_refs == ("document-embedding:" + receipt.job_id,)
        # Replay with the existing common live_reports' minimal response shape.
        assert embedding_step_report(plan, row, {"job_id": receipt.job_id,
            "work_kind": receipt.work_kind, "status": "succeeded"}) == report
    DocumentChunk.objects.filter(pk=manifest.targets[0]["chunk_id"]).update(text="changed")
    assert embedding_step_report(plan, row, receipt_payload(receipt)) == report
    assert not scoped.store.base_queryset().filter(chunk_id=manifest.targets[0]["chunk_id"]).exists()
    assert ExecutorJobReceipt.objects.get(pk=receipt.pk).status == "succeeded"


@pytest.mark.parametrize("error,retryable", [(RuntimeError("GPU OOM"), True), (ValueError("bad input"), False)])
def test_exception_classification_is_owned_by_documents(scoped, error, retryable):
    envelope, manifest = scoped.configure()
    with executing(envelope), patch("documents.embedding.completion.execute_scoped_embedding", side_effect=error):
        receipt = execute_scoped_receipted(envelope)
    assert receipt.status == "failed"
    assert receipt.retryable is retryable
    row, plan = journal(manifest, envelope)
    assert embedding_step_report(plan, row, receipt_payload(receipt)).status == "failed"


def test_incomplete_targets_remain_request_result_not_independent_states(scoped):
    envelope, manifest = scoped.configure()
    delivery = parse_embedding_delivery(envelope["payload"])
    result = build_embedding_result(delivery.command, job_id=envelope["job_id"],
        manifest_id=str(manifest.pk), manifest_fingerprint=manifest.fingerprint,
        target_ids=tuple(target["chunk_id"] for target in manifest.targets),
        outcomes=tuple(TargetOutcome(target["chunk_id"], "failed") for target in manifest.targets))
    with executing(envelope), patch("documents.embedding.completion.execute_scoped_embedding",
            side_effect=lambda envelope, on_completed: on_completed(result)):
        receipt = execute_scoped_receipted(envelope)
    assert receipt.status == "failed" and not receipt.retryable
    assert receipt.result == result
    assert ExecutorJobReceipt.objects.count() == 1


def test_stale_attempt_failure_does_not_change_new_attempt(scoped):
    envelope, _ = scoped.configure()
    def obsolete(_, **kwargs):
        ExecutorJobReceipt.objects.filter(pk=envelope["job_id"]).update(attempt=2)
        raise ExecutionOwnershipLost("old attempt")
    with executing(envelope), patch("documents.embedding.completion.execute_scoped_embedding", side_effect=obsolete):
        with pytest.raises(ExecutionOwnershipLost):
            execute_scoped_receipted(envelope)
    current = ExecutorJobReceipt.objects.get(pk=envelope["job_id"])
    assert current.status == "running" and current.attempt == 2


def test_legacy_recovery_excludes_unfinished_scoped_requests_only(scoped):
    envelope, manifest = scoped.configure()
    assert not list(scoped_recovery_blocked_node_ids())  # Preparation is not execution.
    with executing(envelope), patch("documents.embedding.completion.execute_scoped_embedding", side_effect=RuntimeError("busy")):
        receipt = execute_scoped_receipted(envelope)
    assert list(scoped_recovery_blocked_node_ids()) == [manifest.node_id]
    ExecutorJobReceipt.objects.filter(pk=receipt.pk).update(retryable=False)
    assert list(scoped_recovery_blocked_node_ids()) == [manifest.node_id]
    from files.models import FileBlob
    FileBlob.objects.filter(node_id=manifest.node_id).update(sha256="new-source")
    assert not list(scoped_recovery_blocked_node_ids())


def test_report_rejects_fabricated_response(scoped):
    envelope, manifest = scoped.configure()
    row, plan = journal(manifest, envelope)
    with executing(envelope), patch("documents.embedding.completion.execute_scoped_embedding", side_effect=ValueError("bad")):
        receipt = execute_scoped_receipted(envelope)
    response = {**receipt_payload(receipt), "status": "succeeded"}
    with pytest.raises(WorkContractError):
        embedding_step_report(plan, row, response)


def test_terminal_callback_is_inside_final_transaction_and_rolls_back(scoped):
    from django.db import connection
    from documents.embedding.scoped_execution import execute_scoped_embedding
    from documents.services.executor_jobs import finish_execution
    envelope, _ = scoped.configure()
    with executing(envelope) as receipt, patch("documents.embedding.execution.run_embedding_with_admission",
            side_effect=lambda _, texts: embedding(texts)):
        def completed(result):
            assert connection.in_atomic_block
            finish_execution(receipt, result=result)
            raise RuntimeError("completion transaction failed")
        with pytest.raises(RuntimeError, match="completion transaction failed"):
            execute_scoped_embedding(envelope, on_completed=completed)
    receipt.refresh_from_db()
    assert receipt.status == "running"
    assert receipt.result == {}
