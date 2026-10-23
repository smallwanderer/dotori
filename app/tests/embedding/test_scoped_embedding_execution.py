from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config.enums import AIStatus
from documents.models import ChunkEmbedding, DocumentChunk
from documents.embedding.providers.base import EmbeddingResult
from documents.embedding.stores.pgvector_chunk import PgVectorChunkEmbeddingStore
from documents.embedding.scoped_execution import execute_scoped_embedding
from documents.processing.embedding_contract import EmbeddingDelivery, ChunkSelector
from documents.processing.embedding_manifests import approve_embedding_manifest
from documents.services.executor_jobs import claim_execution, fail_execution, finish_execution
from orchestration.receipts import active_receipt_ownership, ExecutionOwnership, ExecutionOwnershipLost
from orchestration.plans import plan_from_record, plan_record
from tests.processing.test_embedding_preparation import deployment, doc, parsed
from tests.processing.test_embedding_manifests import approval, intent

pytestmark = pytest.mark.django_db


@pytest.fixture
def scoped(approval):
    row, command = approval
    store = PgVectorChunkEmbeddingStore(model_name="fake", backend="fake", dimension=384,
        supports_sparse=False, generation_id="generation", runtime_fingerprint="runtime")
    def configure(mode="refresh", ids=None):
        selected = command.selector if ids is None else ChunkSelector("chunk_ids", tuple(ids))
        configured = replace(command, mode=mode, selector=selected)
        plan = plan_from_record(row.definition)
        row.definition = plan_record(replace(plan, steps=(replace(plan.steps[0], payload=intent(configured)),)))
        row.save(update_fields=["definition"])
        manifest = approve_embedding_manifest(plan_id=row.pk, step_id="embed", command=configured)
        payload = EmbeddingDelivery(configured, str(manifest.pk), manifest.fingerprint).to_payload()
        return {"job_id": manifest.job_id, "work_kind": "embed_document", "payload": payload,
                "plan_id": row.pk, "step_id": "embed"}, manifest
    with patch("documents.embedding.scoped_execution.get_embedding_store_instance", return_value=store), \
         patch("documents.embedding.executor.prepare_embedding_input_text", side_effect=lambda text, **kw: (text, 1, False)), \
         patch("documents.embedding.scoped_execution.prepare_embedding_input_text", side_effect=lambda text, **kw: (text, 1, False)), \
         patch("documents.embedding.executor.get_embedding_document_batch_max_chunks", return_value=1), \
         patch("documents.embedding.executor.get_embedding_document_batch_max_tokens", return_value=100), \
         patch("documents.embedding.executor._log_if_document_ready"):
        yield SimpleNamespace(configure=configure, store=store)


@contextmanager
def executing(envelope):
    claim = claim_execution(job_id=envelope["job_id"], work_kind="embed_document",
        target_key="manifest:" + envelope["payload"]["manifest"]["id"], payload=envelope["payload"])
    token = active_receipt_ownership.set(ExecutionOwnership.capture(claim.receipt))
    try:
        yield claim.receipt
    finally:
        active_receipt_ownership.reset(token)


def embedding(texts):
    return [EmbeddingResult(dense_vector=[0.1] * 384, sparse_vector={},
        model_name="fake", backend="fake") for text in texts]


def test_refresh_ignores_legacy_chunk_work_status_and_preserves_batch_order(scoped):
    envelope, manifest = scoped.configure()
    DocumentChunk.objects.update(status=AIStatus.COMPLETED)
    with executing(envelope), patch("documents.embedding.execution.run_embedding_with_admission",
            side_effect=lambda _, texts: embedding(texts)) as inference:
        result = execute_scoped_embedding(envelope)
    assert result["ready"]
    assert result["counts"]["succeeded"] == manifest.target_count == 2
    assert [call.kwargs["texts"] for call in inference.call_args_list] == [["first"], ["second"]]


def test_explicit_subset_does_not_expand(scoped):
    selected = DocumentChunk.objects.order_by("chunk_index").first().pk
    envelope, _ = scoped.configure(ids=[selected])
    with executing(envelope), patch("documents.embedding.execution.run_embedding_with_admission",
            side_effect=lambda _, texts: embedding(texts)) as inference:
        result = execute_scoped_embedding(envelope)
    assert result["counts"]["requested"] == 1
    assert list(ChunkEmbedding.objects.values_list("chunk_id", flat=True)) == [selected]
    assert inference.call_count == 1


@pytest.mark.parametrize("mode", ["refresh", "missing"])
def test_failed_attempt_restarts_whole_manifest_not_saved_prefix(scoped, mode):
    envelope, _ = scoped.configure(mode=mode)
    first_calls = []
    def fail_second(_, texts):
        first_calls.append(texts)
        if len(first_calls) == 2:
            raise RuntimeError("GPU OOM")
        return embedding(texts)
    with executing(envelope) as receipt:
        with patch("documents.embedding.execution.run_embedding_with_admission", side_effect=fail_second):
            with pytest.raises(RuntimeError, match="GPU OOM"):
                execute_scoped_embedding(envelope)
        fail_execution(receipt, code="EXECUTION_FAILED", message="OOM", retryable=True)
    assert ChunkEmbedding.objects.count() == 1
    with executing(envelope) as receipt, patch("documents.embedding.execution.run_embedding_with_admission",
            side_effect=lambda _, texts: embedding(texts)) as inference:
        result = execute_scoped_embedding(envelope)
        assert receipt.attempt == 2
    assert result["ready"]
    assert inference.call_count == 2
    assert all(row.provenance["attempt"] == 2 for row in ChunkEmbedding.objects.all())


def test_succeeded_redelivery_does_not_infer(scoped):
    envelope, _ = scoped.configure()
    with executing(envelope) as receipt, patch("documents.embedding.execution.run_embedding_with_admission",
            side_effect=lambda _, texts: embedding(texts)):
        result = execute_scoped_embedding(envelope)
        finish_execution(receipt, result=result)
    with executing(envelope), patch("documents.embedding.execution.run_embedding_with_admission") as inference:
        assert execute_scoped_embedding(envelope) == result
        inference.assert_not_called()


def test_owner_required_before_inference(scoped):
    envelope, _ = scoped.configure()
    with pytest.raises(ExecutionOwnershipLost):
        execute_scoped_embedding(envelope)


def test_multiple_texts_still_use_one_model_call_per_batch(scoped):
    envelope, _ = scoped.configure()
    with executing(envelope), patch("documents.embedding.executor.get_embedding_document_batch_max_chunks", return_value=8), \
         patch("documents.embedding.execution.run_embedding_with_admission", side_effect=lambda _, texts: embedding(texts)) as inference:
        result = execute_scoped_embedding(envelope)
    assert result["ready"]
    assert inference.call_count == 1
    assert inference.call_args.kwargs["texts"] == ["first", "second"]


def test_missing_reuses_preexisting_valid_vector_but_refreshes_remaining(scoped):
    from documents.embedding.persistence import capture_batch_evidence
    chunk = DocumentChunk.objects.order_by("chunk_index").first()
    chunk._embedding_input_evidence = capture_batch_evidence([chunk])[chunk.pk]
    scoped.store.save_chunk_embedding(chunk=chunk, embedding=embedding([chunk.text])[0])
    envelope, _ = scoped.configure(mode="missing")
    with executing(envelope), patch("documents.embedding.execution.run_embedding_with_admission",
            side_effect=lambda _, texts: embedding(texts)) as inference:
        result = execute_scoped_embedding(envelope)
    assert result["ready"]
    assert result["counts"]["reused"] == 1
    assert result["counts"]["succeeded"] == 1
    assert inference.call_args.kwargs["texts"] == ["second"]


def test_source_change_during_model_call_refuses_batch_save(scoped, doc):
    from orchestration.execution import StaleExecution
    envelope, _ = scoped.configure()
    def changed(_, texts):
        type(doc.blob).objects.filter(pk=doc.blob.pk).update(sha256="changed")
        return embedding(texts)
    with executing(envelope), patch("documents.embedding.execution.run_embedding_with_admission", side_effect=changed):
        with pytest.raises(StaleExecution):
            execute_scoped_embedding(envelope)
    assert not ChunkEmbedding.objects.exists()
