import json
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from celery.exceptions import Retry
from django.test import RequestFactory

from capabilities.deployment import clear_deployment_snapshot_cache
from capabilities.modules import DOCUMENTS_MODULE
from documents import executor_views, tasks
from documents.orchestration import WorkKind
from documents.models import ExecutorJobReceipt
from documents.services.executor_jobs import claim_execution, fail_execution
from installation.deployment import build_module_deployment_plan, write_deployment_plan


@pytest.fixture(autouse=True)
def active_module_plan(tmp_path, monkeypatch):
    plan = build_module_deployment_plan([DOCUMENTS_MODULE])
    path = write_deployment_plan(plan, repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    clear_deployment_snapshot_cache()
    yield
    clear_deployment_snapshot_cache()


def envelope(job="job"):
    return {"job_id": job, "envelope_version": 1, "work_kind": "parse_document", "payload": {"node_id": 1}}


def test_followup_publish_failure_retries_parent_with_original_deadline():
    deadline = time.time() + 300
    task = SimpleNamespace(request=SimpleNamespace(headers={"executor_deadline": deadline}, retries=0), retry=Mock(side_effect=Retry()))
    with patch("documents.services.executor_transport.dispatch_executor_job", return_value={"result": {"status": "success"}}), patch.object(tasks, "_publish_executor_followups", side_effect=RuntimeError("Redis disconnected")):
        with pytest.raises(Retry):
            tasks._dispatch_or_retry(task, WorkKind.PARSE_DOCUMENT, envelope=envelope(), payload={"node_id": 1})
    assert task.retry.call_args.kwargs["headers"]["executor_deadline"] == deadline
    assert tasks._DISPATCH_SLOTS["parse"].acquire(blocking=False)
    tasks._DISPATCH_SLOTS["parse"].release()


def test_invalid_followup_fails_without_retrying_parent():
    task = SimpleNamespace(request=SimpleNamespace(headers={}, retries=0), retry=Mock())
    response = {
        "result": {"status": "success"},
        "followups": [{"work_kind": "unknown_work", "payload": {"node_id": 1}, "job_id": "child"}],
    }
    with patch("documents.services.executor_transport.dispatch_executor_job", return_value=response):
        result = tasks._dispatch_or_retry(task, WorkKind.PARSE_DOCUMENT, envelope=envelope(), payload={"node_id": 1})
    assert result["error_code"] == "INVALID_FOLLOWUP"
    assert result["retryable"] is False
    task.retry.assert_not_called()


def test_parse_slot_busy_does_not_block_embed_dispatch():
    task = SimpleNamespace(request=SimpleNamespace(headers={}, retries=0), retry=Mock(side_effect=Retry()))
    tasks._DISPATCH_SLOTS["parse"].acquire()
    try:
        with patch("documents.services.executor_transport.dispatch_executor_job", return_value={"result": {"status": "success"}}) as dispatch:
            assert tasks._dispatch_or_retry(task, WorkKind.PLAN_DOCUMENT_EMBEDDING, envelope=envelope(), payload={"node_id": 1})["status"] == "success"
            dispatch.assert_called_once()
    finally:
        tasks._DISPATCH_SLOTS["parse"].release()


@pytest.mark.django_db
def test_overlapping_batch_claims_do_not_execute_together():
    first = claim_execution(job_id="one", work_kind="embed_document_batch", target_key="chunks:1,2", payload={"chunk_ids": [1, 2]}, source_snapshot={"payload": {"chunk_ids": [1, 2]}})
    second = claim_execution(job_id="two", work_kind="embed_document_batch", target_key="chunks:2,3", payload={"chunk_ids": [2, 3]})
    assert first.should_execute and not second.should_execute


@pytest.mark.django_db
def test_domain_attempt_budget_is_bounded():
    args = dict(job_id="one", work_kind="parse_document", target_key="node:1", payload={"node_id": 1})
    for _ in range(4):
        claim = claim_execution(**args)
        assert claim.should_execute
        fail_execution(claim.receipt, code="TEMP", message="temporary", retryable=True)
    final = claim_execution(**args)
    assert not final.should_execute
    assert final.receipt.error["code"] == "RETRIES_EXHAUSTED"


@pytest.mark.django_db
def test_planning_receipt_failure_rolls_back_domain_write(settings, monkeypatch):
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "embedding")
    body = envelope()
    body["work_kind"] = "plan_document_embedding"
    request = RequestFactory().post("/", json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION="Bearer token")
    def plan(_):
        ExecutorJobReceipt.objects.create(job_id="domain-write", work_kind="test", target_key="test")
        return {"status": "success"}, []
    with patch("documents.services.executor_snapshot.capture_snapshot", return_value=({}, {})), patch("documents.processing.work_handlers._execute_embedding", side_effect=plan), patch("orchestration.receipts.finish_execution", side_effect=RuntimeError("commit interrupted")):
        response = executor_views.execute_job(request)
    assert response.status_code == 503
    assert not ExecutorJobReceipt.objects.filter(job_id="domain-write").exists()
    assert ExecutorJobReceipt.objects.get(job_id="job").status == "failed"


@pytest.mark.django_db
def test_invalid_planning_followup_cannot_commit_success_receipt(settings, monkeypatch):
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "embedding")
    body = envelope()
    body["work_kind"] = "plan_document_embedding"
    request = RequestFactory().post("/", json.dumps(body), content_type="application/json", HTTP_AUTHORIZATION="Bearer token")
    invalid = [{"work_kind": "unknown_work", "payload": {"node_id": 1}, "job_id": "child"}]
    with patch("documents.services.executor_snapshot.capture_snapshot", return_value=({}, {})), patch(
        "documents.processing.work_handlers._execute_embedding", return_value=({"status": "success"}, invalid)
    ), patch("orchestration.receipts.finish_execution") as finish:
        response = executor_views.execute_job(request)
    assert response.status_code == 503
    finish.assert_not_called()
    assert ExecutorJobReceipt.objects.get(job_id="job").status == "failed"


def test_executor_busy_leaves_status_thread_available(settings):
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    request = RequestFactory().post("/", "{}", content_type="application/json", HTTP_AUTHORIZATION="Bearer token")
    executor_views._JOB_SLOT.acquire()
    try:
        response = executor_views.execute_job(request)
        assert response.status_code == 503
        assert response["Retry-After"] == "5"
    finally:
        executor_views._JOB_SLOT.release()


def test_response_loss_replay_reads_receipt_without_reexecuting():
    from documents.services.executor_transport import dispatch_executor_job
    with patch("orchestration.transport.read_executor_job", return_value={"status": "succeeded", "result": {"status": "success"}}), patch("orchestration.transport.requests.post") as post:
        result = dispatch_executor_job(WorkKind.PARSE_DOCUMENT, envelope={**envelope(), "attempt": 2}, payload={"node_id": 1})
    assert result["status"] == "succeeded"
    post.assert_not_called()


def test_extract_only_parse_does_not_create_embedding_followup():
    task_envelope = envelope()
    with patch(
        "documents.services.capability_admission.capability_enabled",
        return_value=False,
    ), patch(
        "documents.processing.parsing.get_parser_for_execution",
        return_value=object(),
    ), patch(
        "documents.processing.parsing.execute_parse_node",
        return_value={"status": "success", "node_id": 1, "chunk_count": 2},
    ):
        result, followups = executor_views._execute_parser(task_envelope)

    assert result["status"] == "success"
    assert followups == []


def test_stale_snapshot_prevents_domain_persistence(monkeypatch):
    """An executor must reject work whose document/runtime changed in flight."""
    from documents.services import executor_snapshot

    task_envelope = envelope()
    expected_source = {"payload": task_envelope["payload"], "revision": 1}
    expected_runtime = {"fingerprint": "runtime-a"}
    token = executor_snapshot.active_execution.set(
        (task_envelope, expected_source, expected_runtime)
    )
    try:
        monkeypatch.setattr(
            executor_snapshot,
            "capture_snapshot",
            lambda _envelope: ({"payload": task_envelope["payload"], "revision": 2}, expected_runtime),
        )
        with pytest.raises(executor_snapshot.StaleExecution):
            executor_snapshot.validate_execution()
    finally:
        executor_snapshot.active_execution.reset(token)


@pytest.mark.django_db
def test_extract_only_snapshot_does_not_require_embedding_runtime(tmp_path, monkeypatch):
    from capabilities.deployment import clear_deployment_snapshot_cache
    from capabilities.modules import DOCUMENTS_MODULE
    from documents.services import executor_snapshot
    from installation.deployment import build_module_deployment_plan, write_deployment_plan

    plan = build_module_deployment_plan(
        [DOCUMENTS_MODULE], disabled_provider_roles=("embedding", "llm")
    )
    plan_path = write_deployment_plan(plan, repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(plan_path))
    clear_deployment_snapshot_cache()
    monkeypatch.setattr(
        executor_snapshot,
        "load_embedding_runtime",
        Mock(side_effect=AssertionError("embedding runtime must not be read")),
    )

    _source, runtime = executor_snapshot.capture_snapshot(envelope())

    assert runtime["chunking_strategy"] == "hierarchical"
    assert runtime["capability_revision"].startswith("cap-")


def test_legacy_message_uses_celery_request_id():
    tasks.parse_document_with_docling.push_request(id="legacy-fixed-id")
    try:
        with patch("documents.tasks._embedding_queue_backpressure", return_value=(False, 0, 1)), patch("documents.services.executor_transport.dispatch_executor_job", return_value={"result": {"status": "success"}}) as dispatch:
            tasks.parse_document_with_docling(1)
    finally:
        tasks.parse_document_with_docling.pop_request()
    assert dispatch.call_args.kwargs["envelope"]["job_id"] == "legacy-fixed-id"
