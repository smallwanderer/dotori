"""Registered handlers, shared context, and commit-safe upload production."""
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.db import transaction
from django.test import RequestFactory

from capabilities import execution, work_registry
from capabilities.deployment import clear_deployment_snapshot_cache
from installation.deployment import build_module_deployment_plan, write_deployment_plan
from orchestration import executor_views, handlers, receipts
from orchestration.execution import active_execution, active_snapshot_reader, StaleExecution


@pytest.fixture(autouse=True)
def active_module_plan(tmp_path, monkeypatch):
    path = write_deployment_plan(build_module_deployment_plan(["documents"]), repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    clear_deployment_snapshot_cache()
    yield
    clear_deployment_snapshot_cache()


def request(body=None, token="token"):
    body = body or {"job_id": "job", "envelope_version": 1,
                    "work_kind": "parse_document", "payload": {"node_id": 1}}
    return RequestFactory().post("/", json.dumps(body), content_type="application/json",
                                 HTTP_AUTHORIZATION=f"Bearer {token}")


def test_context_and_exception_identity():
    from documents.services import executor_snapshot, executor_jobs
    assert executor_snapshot.active_execution is active_execution
    assert executor_snapshot.StaleExecution is StaleExecution
    assert executor_jobs.ExecutorJobConflict is receipts.ExecutorJobConflict
    assert executor_jobs.ExecutionClaim is receipts.ExecutionClaim
    from documents import executor_views as legacy
    assert legacy.execute_job is executor_views.execute_job
    assert legacy._JOB_SLOT is executor_views._JOB_SLOT


def test_role_mismatch_does_not_load_handler():
    with patch.object(handlers, "_load") as load:
        with pytest.raises(ValueError):
            handlers.get_handler("parse_document", "embedding")
    load.assert_not_called()


@pytest.mark.django_db
def test_second_module_registers_without_document_enum_or_executor_branch(settings, monkeypatch):
    """A non-document handler and repository use the same HTTP execution path."""
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "test-role")
    definition = work_registry.WorkDefinition(
        "test_media", "test.capability", "test-role", "test", "test.task", ("media_id",),
        f"{__name__}.TEST_HANDLER",
    )
    spec = execution.ExecutionRole("test-role", "test", "test", receipt_repository_path=f"{__name__}.TEST_REPOSITORY")
    monkeypatch.setattr(work_registry, "WORK_REGISTRY", {**work_registry.WORK_REGISTRY, definition.kind: definition})
    monkeypatch.setattr(execution, "EXECUTION_ROLES", {**execution.EXECUTION_ROLES, spec.role: spec})
    handler = SimpleNamespace(
        kind=definition.kind, atomic_execution=False,
        target_key=Mock(return_value="media:3"), capture_snapshot=Mock(return_value=({}, {})),
        execute=Mock(return_value=({"status": "success", "artifact_ref": "media:3"}, [])),
    )
    receipt = SimpleNamespace(job_id="media-job", work_kind=definition.kind, status="succeeded",
                              target_key="media:3", payload_fingerprint="test-payload",
                              attempt=1, result={"status": "success", "artifact_ref": "media:3"},
                              error={}, retryable=False, followups=[])
    repository = Mock()
    repository.complete.return_value = receipt
    monkeypatch.setattr(sys.modules[__name__], "TEST_HANDLER", handler, raising=False)
    monkeypatch.setattr(sys.modules[__name__], "TEST_REPOSITORY", repository, raising=False)
    body = {"job_id": "media-job", "envelope_version": 1, "work_kind": definition.kind, "payload": {"media_id": 3}}
    sentinel = active_execution.set("outer-context")
    try:
        with patch.object(executor_views, "require_registered_work", return_value=definition), patch.object(
            receipts, "claim_execution", return_value=receipts.ExecutionClaim(receipt, True)
        ):
            response = executor_views.execute_job(request(body))
        assert response.status_code == 200
        assert json.loads(response.content)["result"]["artifact_ref"] == "media:3"
        handler.execute.assert_called_once_with(body)
        assert active_execution.get() == "outer-context"
        assert active_snapshot_reader.get() is None
    finally:
        active_execution.reset(sentinel)


@pytest.mark.parametrize("field,value", [("work_kind", []), ("envelope_version", 2), ("payload", [])])
def test_malformed_request_does_not_claim(settings, monkeypatch, field, value):
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "parser")
    body = {"job_id": "job", "envelope_version": 1, "work_kind": "parse_document", "payload": {"node_id": 1}}
    body[field] = value
    with patch.object(receipts, "claim_execution") as claim:
        assert executor_views.execute_job(request(body)).status_code == 422
    claim.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_upload_is_not_published_before_commit_or_after_rollback(django_db_blocker):
    from documents.signals import trigger_document_parsing
    with django_db_blocker.unblock(), patch("documents.signals._publish_uploaded_blob") as publish:
        blob = SimpleNamespace(node_id=1)
        with transaction.atomic():
            trigger_document_parsing(None, blob, True)
            publish.assert_not_called()
        publish.assert_called_once_with(blob)
        publish.reset_mock()
        with pytest.raises(RuntimeError), transaction.atomic():
            trigger_document_parsing(None, blob, True)
            raise RuntimeError("rollback")
        publish.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_upload_has_only_one_initial_producer(django_db_blocker):
    from documents.signals import trigger_document_parsing
    blob = SimpleNamespace(node_id=1, node=SimpleNamespace(ai_processing_enabled=True),
                           original_name="file.pdf", file=SimpleNamespace(name="file.pdf"), mime_type="application/pdf")
    with django_db_blocker.unblock(), patch("documents.signals.capability_enabled", return_value=True), patch(
        "documents.signals.enqueue_parse"
    ) as publish:
        with transaction.atomic():
            trigger_document_parsing(None, blob, True)
            trigger_document_parsing(None, blob, False)
            publish.assert_not_called()
        assert publish.call_count == 1


@pytest.mark.django_db(transaction=True)
def test_publish_failure_preserves_blob_as_recovery_input():
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from files.models import FileBlob, Node
    from documents.processing.recovery import get_parse_recovery_node_ids
    user = get_user_model().objects.create_user(email="publish-recovery@example.com", password="test")
    node = Node.objects.create(owner=user, name="recovery.txt", ext=".txt", node_type="file")
    with patch("documents.signals.enqueue_parse", side_effect=RuntimeError("broker unavailable")):
        with transaction.atomic():
            blob = FileBlob.objects.create(node=node, original_name="recovery.txt", mime_type="text/plain",
                file=SimpleUploadedFile("recovery.txt", b"text"), size=4, status="ready")
    assert FileBlob.objects.filter(pk=blob.pk).exists()
    assert node.id in get_parse_recovery_node_ids(100)
    from documents.processing.recovery import recover_document_pipeline_backlog_sync
    with patch("documents.processing.recovery.recovery_redis_client", return_value=None), patch(
        "documents.processing.recovery.get_embedding_recovery_chunk_ids", return_value=[]
    ), patch("documents.processing.recovery.enqueue_parse") as publish:
        summary = recover_document_pipeline_backlog_sync()
    publish.assert_called_once()
    assert publish.call_args.args == (node.id,)
    assert summary["parse_requeued"] == 1


@pytest.mark.django_db
def test_http_replay_and_status_use_existing_receipt(settings, monkeypatch):
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "parser")
    with patch("documents.services.executor_snapshot.capture_snapshot", return_value=({}, {})), patch(
        "documents.processing.work_handlers._execute_parser", return_value=({"status": "success"}, [])
    ) as execute:
        first = executor_views.execute_job(request())
        replay = executor_views.execute_job(request())
    assert first.status_code == replay.status_code == 200
    assert json.loads(first.content) == json.loads(replay.content)
    execute.assert_called_once()
    status_request = RequestFactory().get("/", HTTP_AUTHORIZATION="Bearer token")
    assert json.loads(executor_views.job_status(status_request, "job").content) == json.loads(first.content)


@pytest.mark.django_db
def test_parse_receipt_failure_rolls_back_chunks(settings, monkeypatch):
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from files.models import FileBlob, Node
    from documents.models import DocumentChunk, DocumentParseResult, ExecutorJobReceipt
    from documents.parsers.schema import ParseResult, ChunkPayload
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "parser")
    user = get_user_model().objects.create_user(email="parse-rollback@example.com", password="test")
    node = Node.objects.create(owner=user, name="parse.txt", ext=".txt", node_type="file")
    FileBlob.objects.create(node=node, original_name="parse.txt", mime_type="text/plain",
        file=SimpleUploadedFile("parse.txt", b"text"), size=4, status="ready")
    parser = Mock()
    parser.parse.return_value = ParseResult(parser_mode="test", parser_backend="test",
        file_path="parse.txt", file_ext=".txt", status="success",
        chunks=[ChunkPayload(chunk_index=0, serialized_text="text")])
    body = {"job_id": "parse-rollback", "envelope_version": 1, "work_kind": "parse_document", "payload": {"node_id": node.id}}
    with patch("documents.services.executor_snapshot.capture_snapshot", return_value=({}, {})), patch(
        "documents.processing.parsing.get_parser_for_execution", return_value=parser
    ), patch("documents.processing.parsing._parse_metadata", return_value={}), patch(
        "documents.processing.work_handlers.finish_execution", side_effect=RuntimeError("receipt write failed")
    ):
        response = executor_views.execute_job(request(body))
    assert response.status_code == 503
    assert not DocumentChunk.objects.filter(parse_result__node=node).exists()
    assert not DocumentParseResult.objects.filter(node=node, status="completed").exists()
    assert ExecutorJobReceipt.objects.get(job_id="parse-rollback").status == "failed"


@pytest.mark.django_db
def test_parse_revalidates_source_inside_locked_save_transaction():
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from files.models import FileBlob, Node
    from documents.models import DocumentParseResult
    from documents.parsers.schema import ParseResult
    from documents.processing.parsing import save_parse_result
    user = get_user_model().objects.create_user(email="parse-stale@example.com", password="test")
    node = Node.objects.create(owner=user, name="stale.txt", ext=".txt", node_type="file")
    FileBlob.objects.create(node=node, original_name="stale.txt", mime_type="text/plain",
        file=SimpleUploadedFile("stale.txt", b"text"), size=4, status="ready")
    envelope = {"payload": {"node_id": node.id}}
    token = active_execution.set((envelope, {"revision": 1}, {}))
    result = ParseResult(parser_mode="test", parser_backend="test", file_path="stale.txt", file_ext=".txt", status="success", chunks=[])
    try:
        with patch("documents.processing.parsing._parse_metadata", return_value={}), patch(
            "documents.services.executor_snapshot.capture_snapshot",
            side_effect=[({"revision": 1}, {}), ({"revision": 2}, {})],
        ), pytest.raises(StaleExecution):
            save_parse_result(node, result)
        assert not DocumentParseResult.objects.filter(node=node).exists()
    finally:
        active_execution.reset(token)
