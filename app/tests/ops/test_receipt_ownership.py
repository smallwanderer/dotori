import pytest

from documents.services.executor_jobs import claim_execution, finish_execution, fail_execution
from documents.services.receipt_repository import REPOSITORY
from orchestration.receipts import (
    ExecutionOwnership, ExecutionOwnershipLost, active_receipt_ownership,
)

pytestmark = pytest.mark.django_db


def claim():
    return claim_execution(job_id="ownership-job", work_kind="parse_document",
                           target_key="node:1", payload={"node_id": 1})


@pytest.mark.parametrize("terminal", ["complete", "fail"])
def test_previous_attempt_cannot_overwrite_restarted_receipt(terminal):
    first = claim()
    fail_execution(first.receipt, code="TEMPORARY", message="retry", retryable=True)
    second = claim()
    assert second.receipt.attempt == 2
    with pytest.raises(ExecutionOwnershipLost):
        if terminal == "complete":
            finish_execution(first.receipt, result={"status": "success"})
        else:
            fail_execution(first.receipt, code="OLD", message="old failure", retryable=True)
    current = REPOSITORY.get(first.receipt.job_id)
    assert (current.status, current.attempt, current.error) == ("running", 2, {})


def test_terminal_success_is_idempotent_but_cannot_be_replaced_or_failed():
    receipt = claim().receipt
    result = {"status": "success"}
    completed = finish_execution(receipt, result=result)
    replay = finish_execution(receipt, result=result)
    assert replay.finished_at == completed.finished_at
    with pytest.raises(ExecutionOwnershipLost):
        finish_execution(receipt, result={"status": "different"})
    with pytest.raises(ExecutionOwnershipLost):
        fail_execution(receipt, code="LATE", message="late", retryable=True)
    assert REPOSITORY.get(receipt.job_id).result == result


def test_active_owner_survives_handler_receipt_reload():
    first = claim()
    token = active_receipt_ownership.set(ExecutionOwnership.capture(first.receipt))
    try:
        # Simulate a later attempt being visible to a handler's fresh ORM read.
        REPOSITORY.model.objects.filter(pk=first.receipt.pk).update(attempt=2)
        refreshed = REPOSITORY.get(first.receipt.job_id)
        with pytest.raises(ExecutionOwnershipLost):
            finish_execution(refreshed, result={"status": "success"})
    finally:
        active_receipt_ownership.reset(token)
    assert REPOSITORY.get(first.receipt.job_id).status == "running"


@pytest.mark.parametrize("field,value", [
    ("payload_fingerprint", "changed"), ("target_key", "node:2"),
    ("work_kind", "embed_document_batch"),
])
def test_changed_identity_cannot_be_completed(field, value):
    receipt = claim().receipt
    REPOSITORY.model.objects.filter(pk=receipt.pk).update(**{field: value})
    with pytest.raises(ExecutionOwnershipLost):
        finish_execution(receipt, result={"status": "success"})


def test_retry_budget_exhaustion_still_terminalizes_failed_receipt():
    for attempt in range(1, 5):
        receipt = claim().receipt
        assert receipt.attempt == attempt
        fail_execution(receipt, code="TEMPORARY", message="retry", retryable=True)
    exhausted = claim()
    assert not exhausted.should_execute
    assert exhausted.receipt.status == "failed"
    assert not exhausted.receipt.retryable
    assert exhausted.receipt.error["code"] == "RETRIES_EXHAUSTED"


def test_http_ownership_loss_is_conflict_and_restores_context(settings, monkeypatch):
    import json
    from django.test import RequestFactory
    from orchestration import executor_views

    settings.EXECUTOR_INTERNAL_TOKEN = "ownership-test-token"
    def stale(_request):
        active_receipt_ownership.set("inner")
        raise ExecutionOwnershipLost("stale attempt")
    monkeypatch.setattr(executor_views, "_execute_job", stale)
    token = active_receipt_ownership.set("outer")
    try:
        request = RequestFactory().post("/", HTTP_AUTHORIZATION="Bearer ownership-test-token")
        response = executor_views.execute_job(request)
        assert response.status_code == 409
        assert json.loads(response.content)["error"]["code"] == "EXECUTION_OWNERSHIP_LOST"
        assert active_receipt_ownership.get() == "outer"
    finally:
        active_receipt_ownership.reset(token)
