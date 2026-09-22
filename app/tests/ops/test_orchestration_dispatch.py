from unittest.mock import patch
from types import SimpleNamespace

import pytest

from capabilities.config import write_capability_config
from document_ai.services.capability_admission import CapabilityAdmissionError
from document_ai.orchestration import TASK_ENVELOPE_VERSION, TASK_ROUTES, WorkKind


def assert_enveloped_call(mock, *, args, queue, work_kind, trace_id, parent_job_id=None):
    call = mock.call_args
    assert call.kwargs["args"] == args
    assert call.kwargs["queue"] == queue
    task_kwargs = call.kwargs["kwargs"]
    assert task_kwargs["envelope_version"] == TASK_ENVELOPE_VERSION
    assert task_kwargs["work_kind"] == work_kind.value
    assert task_kwargs["trace_id"] == trace_id
    assert task_kwargs["parent_job_id"] == parent_job_id
    assert task_kwargs["enqueued_at"]
    assert task_kwargs["job_id"] == call.kwargs["task_id"]


def test_dispatch_facade_delegates_to_current_celery_tasks():
    from document_ai.orchestration import enqueue_embedding, enqueue_parse

    with (
        patch("document_ai.tasks.parse_document_with_docling.apply_async") as parse,
        patch("document_ai.tasks.enqueue_embedding_tasks.apply_async") as embed,
        patch("document_ai.tasks.embedding_document_batch_with_bge.apply_async") as embed_batch,
        patch("document_ai.tasks.run_quality_evaluation_task.apply_async") as evaluation,
    ):
        from document_ai.orchestration import (
            enqueue_embedding_batch,
            enqueue_retrieval_evaluation,
        )

        enqueue_parse(1, trace_id="parse")
        enqueue_embedding(2, trace_id="embed")
        enqueue_embedding_batch([20, 21], trace_id="embed-batch")
        enqueue_retrieval_evaluation("run-1", trace_id="evaluation")

    assert_enveloped_call(
        parse,
        args=[1],
        queue="parse",
        work_kind=WorkKind.PARSE_DOCUMENT,
        trace_id="parse",
    )
    assert_enveloped_call(
        embed,
        args=[2],
        queue="embed",
        work_kind=WorkKind.PLAN_DOCUMENT_EMBEDDING,
        trace_id="embed",
    )
    assert_enveloped_call(
        embed_batch,
        args=[[20, 21]],
        queue="embed",
        work_kind=WorkKind.EMBED_DOCUMENT_BATCH,
        trace_id="embed-batch",
    )
    assert_enveloped_call(
        evaluation,
        args=["run-1"],
        queue="embed",
        work_kind=WorkKind.EVALUATE_RETRIEVAL,
        trace_id="evaluation",
    )


def test_dispatch_preserves_explicit_job_lineage():
    from document_ai.orchestration import enqueue_embedding_batch

    with patch("document_ai.tasks.embedding_document_batch_with_bge.apply_async") as task:
        enqueue_embedding_batch(
            [10],
            job_id="job-10",
            parent_job_id="planner-5",
            trace_id="trace-1",
            enqueued_at="2026-09-06T12:00:00+09:00",
        )

    task.assert_called_once_with(
        args=[[10]],
        kwargs={
            "envelope_version": TASK_ENVELOPE_VERSION,
            "job_id": "job-10",
            "parent_job_id": "planner-5",
            "trace_id": "trace-1",
            "work_kind": WorkKind.EMBED_DOCUMENT_BATCH.value,
            "enqueued_at": "2026-09-06T12:00:00+09:00",
        },
        queue="embed",
        task_id="job-10",
    )


def test_route_contract_matches_worker_queues_and_task_names():
    assert TASK_ROUTES[WorkKind.PARSE_DOCUMENT].queue == "parse"
    assert TASK_ROUTES[WorkKind.PLAN_DOCUMENT_EMBEDDING].queue == "embed"
    assert TASK_ROUTES[WorkKind.EMBED_DOCUMENT_BATCH].queue == "embed"
    assert TASK_ROUTES[WorkKind.EVALUATE_RETRIEVAL].queue == "embed"
    assert all(route.task_name.startswith("document_ai.tasks.") for route in TASK_ROUTES.values())


def test_executor_followup_uses_registered_route_and_preserves_job_lineage():
    from document_ai.tasks import _publish_executor_followups

    response = {"followups": [{
        "work_kind": "embed_document_batch",
        "payload": {"chunk_ids": [20, 21]},
        "job_id": "child-1",
        "parent_job_id": "parent-1",
    }]}
    with patch("capabilities.work_dispatch.require_capability"), patch(
        "capabilities.work_dispatch.require_registered_work"
    ), patch("celery.current_app.send_task") as send:
        _publish_executor_followups(response)

    call = send.call_args
    assert call.args == ("document_ai.tasks.embedding_document_batch_with_bge",)
    assert call.kwargs["args"] == [[20, 21]]
    assert call.kwargs["queue"] == "embed"
    assert call.kwargs["task_id"] == "child-1"
    assert call.kwargs["kwargs"]["parent_job_id"] == "parent-1"
    assert call.kwargs["kwargs"]["work_kind"] == "embed_document_batch"


def test_executor_followup_rejects_undeclared_payload_without_publication():
    from document_ai.tasks import _publish_executor_followups

    response = {"followups": [{
        "work_kind": "plan_document_embedding",
        "payload": {"node_id": 1, "unexpected": True},
        "job_id": "child-1",
    }]}
    with patch("capabilities.work_dispatch.require_capability"), patch(
        "capabilities.work_dispatch.require_registered_work"
    ), patch("celery.current_app.send_task") as send:
        with pytest.raises(ValueError, match="Invalid payload fields"):
            _publish_executor_followups(response)
    send.assert_not_called()


def test_unknown_later_followup_prevents_partial_publication():
    from document_ai.tasks import _publish_executor_followups
    from capabilities.work_dispatch import WorkContractError

    response = {"followups": [
        {"work_kind": "plan_document_embedding", "payload": {"node_id": 1}, "job_id": "known"},
        {"work_kind": "unknown_work", "payload": {"node_id": 1}, "job_id": "unknown"},
    ]}
    with patch("celery.current_app.send_task") as send:
        with pytest.raises(WorkContractError, match="Unknown work kind"):
            _publish_executor_followups(response)
    send.assert_not_called()


def test_dispatch_rejects_disabled_capability_before_publication(tmp_path, monkeypatch):
    config_path = tmp_path / "capabilities.json"
    write_capability_config([], path=config_path)
    monkeypatch.setenv("DOTORI_CAPABILITIES_CONFIG_PATH", str(config_path))

    from document_ai.orchestration import enqueue_parse

    with patch("document_ai.tasks.parse_document_with_docling.apply_async") as task:
        with pytest.raises(CapabilityAdmissionError) as error:
            enqueue_parse(1)

    assert error.value.code == "CAPABILITY_DISABLED"
    task.assert_not_called()


def test_consumer_finalizes_queued_work_when_capability_was_disabled():
    from document_ai.tasks import _dispatch_or_retry

    task = SimpleNamespace(request=SimpleNamespace(headers={}, retries=0))
    error = CapabilityAdmissionError(
        "documents.extract",
        "CAPABILITY_DISABLED",
        "Document extraction is disabled",
    )
    with patch(
        "document_ai.services.capability_admission.require_capability",
        side_effect=error,
    ), patch(
        "document_ai.processing.capability_state.finalize_capability_rejection"
    ) as finalize:
        result = _dispatch_or_retry(
            task,
            WorkKind.PARSE_DOCUMENT,
            envelope={"job_id": "job-1"},
            payload={"node_id": 7},
        )

    assert result["status"] == "failed"
    assert result["error_code"] == "CAPABILITY_DISABLED"
    assert result["retryable"] is False
    finalize.assert_called_once_with(
        WorkKind.PARSE_DOCUMENT.value,
        {"node_id": 7},
        error_code="CAPABILITY_DISABLED",
        message="Document extraction is disabled",
    )
