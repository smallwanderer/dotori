"""Embedding evidence -> modality-neutral StepReport. No report journal writes."""
from django.db import transaction
from dataclasses import asdict

from documents.models import ExecutorJobReceipt
from documents.processing.embedding_contract import (
    RESULT_SCHEMA, parse_embedding_delivery,
)
from orchestration.command_inputs import stored_command
from orchestration.contracts import WorkContractError
from orchestration.plans import command_for_step, report_from_receipt
from orchestration.receipts import canonical_fingerprint, receipt_payload


def embedding_step_report(plan, row, response):
    try:
        return _report(plan, row, response)
    except (ValueError, KeyError, TypeError, StopIteration, ExecutorJobReceipt.DoesNotExist) as exc:
        raise WorkContractError(str(exc)) from exc


def _report(plan, row, response):
    step = next(step for step in plan.steps if step.work_kind == "embed_document"
        and command_for_step(plan, step.step_id).job_id == response.get("job_id"))
    command = stored_command(row, plan, step)
    delivery = parse_embedding_delivery(command.payload)
    with transaction.atomic():
        receipt = ExecutorJobReceipt.objects.select_for_update().get(pk=command.job_id)
        if (response.get("work_kind") != command.work_kind
                or receipt.work_kind != command.work_kind
                or receipt.payload_fingerprint != canonical_fingerprint(command.payload)
                or response.get("status") != receipt.status
                or ("attempt" in response and response["attempt"] != receipt.attempt)
                or ("result" in response and response["result"] != receipt.result)):
            raise WorkContractError("Receipt response differs from durable execution evidence")
        if receipt.status != "succeeded":
            return report_from_receipt(plan, step.step_id, receipt_payload(receipt))
        if receipt.status == "succeeded":
            result = receipt.result
            expected = {"schema": RESULT_SCHEMA, "job_id": receipt.job_id,
                "manifest_id": delivery.manifest_id, "manifest_fingerprint": delivery.manifest_fingerprint,
                "intent_fingerprint": delivery.command.intent_fingerprint(),
                "source": asdict(delivery.command.source), "input": asdict(delivery.command.input),
                "runtime": asdict(delivery.command.runtime), "mode": delivery.command.mode,
                "ready": True, "status": "ready"}
            if any(result.get(key) != value for key, value in expected.items()):
                raise WorkContractError("Success receipt lacks the approved completion record")
            source = delivery.command.source
            artifact = {"id": "document-embedding:" + receipt.job_id,
                "schema": RESULT_SCHEMA, "status": "ready", "stale": False,
                "source": {"id": source.uid, "revision": source.revision, "checksum": source.checksum}}
            return report_from_receipt(plan, step.step_id, receipt_payload(receipt), (artifact,))
