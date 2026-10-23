from unittest.mock import patch
import uuid
import pytest

from config.enums import AIStatus
from documents.models import ChunkEmbedding, DocumentChunk
from documents.embedding.persistence import capture_batch_evidence, guarded_embedding_write
from documents.embedding.stores.pgvector_chunk import PgVectorChunkEmbeddingStore
from documents.embedding.providers.base import EmbeddingResult
from orchestration.execution import StaleExecution
from orchestration.receipts import ExecutionOwnership, ExecutionOwnershipLost, active_receipt_ownership
from documents.services.executor_jobs import claim_execution
from tests.processing.test_embedding_preparation import deployment, doc, parsed

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def isolated_runtime_cache():
    from documents.services.embedding_runtime_config import get_active_embedding_runtime
    get_active_embedding_runtime.cache_clear()
    yield
    get_active_embedding_runtime.cache_clear()


@pytest.fixture
def inputs(doc):
    result = parsed(doc)
    chunk = result.chunks.first()
    store = PgVectorChunkEmbeddingStore(model_name="fake", backend="fake", dimension=384,
        supports_sparse=False, generation_id="generation", runtime_fingerprint="runtime")
    embedding = EmbeddingResult(dense_vector=[0.1] * 384, sparse_vector={}, model_name="fake", backend="fake")
    evidence = capture_batch_evidence([chunk])
    chunk._embedding_input_evidence = evidence[chunk.pk]
    return chunk, store, embedding, evidence


def save(inputs):
    chunk, store, embedding, evidence = inputs
    with guarded_embedding_write([chunk], evidence):
        store.save_chunk_embedding(chunk=chunk, embedding=embedding)


def test_stamp_filters_stale_content_and_runtime_without_document_ready_marker(inputs):
    chunk, store, _, _ = inputs
    save(inputs)
    assert store.base_queryset().filter(chunk=chunk).exists()
    stamp = ChunkEmbedding.objects.get(chunk=chunk).provenance
    assert stamp["parse_revision"] == str(chunk.parse_result.parse_revision)
    assert stamp["generation_id"] == "generation"
    store.runtime_fingerprint = "different"
    assert not store.base_queryset().exists()
    store.runtime_fingerprint = "runtime"
    DocumentChunk.objects.filter(pk=chunk.pk).update(text="changed")
    assert not store.base_queryset().exists()


@pytest.mark.parametrize("mutation", ["text", "parse", "blob", "disabled", "metadata"])
def test_changed_input_rejects_write(inputs, doc, mutation):
    chunk, _, _, _ = inputs
    if mutation == "text":
        DocumentChunk.objects.filter(pk=chunk.pk).update(text="changed")
    elif mutation == "parse":
        type(chunk.parse_result).objects.filter(pk=chunk.parse_result_id).update(parse_revision=uuid.uuid4())
    elif mutation == "blob":
        type(doc.blob).objects.filter(pk=doc.blob.pk).update(sha256="changed")
    elif mutation == "metadata":
        DocumentChunk.objects.filter(pk=chunk.pk).update(chunk_meta={"changed": True})
    else:
        type(doc.node).objects.filter(pk=doc.node.pk).update(ai_processing_enabled=False)
    with pytest.raises(StaleExecution):
        save(inputs)
    assert not ChunkEmbedding.objects.exists()


def test_new_attempt_rejects_old_vector_writer(inputs):
    chunk, _, _, _ = inputs
    receipt = claim_execution(job_id="old-writer", work_kind="embed_document_batch",
        target_key="chunks:1", payload={"chunk_ids": [chunk.pk]}).receipt
    token = active_receipt_ownership.set(ExecutionOwnership.capture(receipt))
    try:
        type(receipt).objects.filter(pk=receipt.pk).update(attempt=2)
        with pytest.raises(ExecutionOwnershipLost):
            save(inputs)
    finally:
        active_receipt_ownership.reset(token)
    assert not ChunkEmbedding.objects.exists()


def test_failed_refresh_preserves_vector_and_searchability(inputs):
    from documents.embedding.executor import _mark_embedding_failed
    chunk, store, _, _ = inputs
    save(inputs)
    before = ChunkEmbedding.objects.get(chunk=chunk)
    with patch("documents.embedding.store_registry.get_embedding_store_instance", return_value=store):
        _mark_embedding_failed(chunk, embedding_model="fake", embedding_backend="fake",
            error_message="GPU OOM", performance_metrics={})
    after = ChunkEmbedding.objects.get(chunk=chunk)
    assert list(after.vector_384) == list(before.vector_384)
    assert after.provenance == before.provenance
    assert after.status == AIStatus.COMPLETED
    assert store.base_queryset().filter(chunk=chunk).exists()


def test_null_provenance_retains_legacy_document_markers(inputs):
    chunk, store, embedding, _ = inputs
    del chunk._embedding_input_evidence
    store.save_chunk_embedding(chunk=chunk, embedding=embedding)
    assert not store.base_queryset().exists()
    chunk.status = AIStatus.COMPLETED
    chunk.save(update_fields=["status"])
    result = chunk.parse_result
    result.embedding_generation_id = "generation"
    result.embedding_runtime_fingerprint = "runtime"
    result.save(update_fields=["embedding_generation_id", "embedding_runtime_fingerprint"])
    assert store.base_queryset().exists()


def test_runtime_change_before_commit_rolls_back_batch(inputs):
    from types import SimpleNamespace
    chunk, store, embedding, evidence = inputs
    runtimes = [SimpleNamespace(generation_id="generation", runtime_fingerprint="runtime"),
                SimpleNamespace(generation_id="changed", runtime_fingerprint="changed")]
    with patch("documents.services.embedding_runtime_config.load_embedding_runtime", side_effect=runtimes):
        with pytest.raises(StaleExecution):
            with guarded_embedding_write([chunk], evidence):
                store.save_chunk_embedding(chunk=chunk, embedding=embedding)
    assert not ChunkEmbedding.objects.exists()


def test_vector_replacement_invalidates_segment_cache(inputs):
    from documents.models import ChunkSegmentEmbedding
    chunk, _, _, _ = inputs
    save(inputs)
    ChunkSegmentEmbedding.objects.create(chunk=chunk, window_size=2, segment_index=0,
        text="first", vector=[0.1] * 1024)
    save(inputs)
    assert not chunk.segment_embeddings.exists()


def test_segment_runtime_change_regenerates_cached_segments(inputs):
    from documents.search.compression import EmbeddingContextualCompressor
    from types import SimpleNamespace
    chunk, _, _, _ = inputs
    # No stamped parent keeps compatibility with standalone segment callers.
    with patch("documents.search.compression.get_embedding_model", return_value="fake"), \
         patch("documents.search.compression.get_embedding_backend", return_value="fake"), \
         patch("documents.search.compression.get_active_embedding_runtime", return_value=SimpleNamespace(dimension=1024)):
        compressor = EmbeddingContextualCompressor(enabled=True)
    embedding = SimpleNamespace(dense_vector=[0.1] * 1024, sparse_vector={})
    with patch("documents.embedding.embeding_models.embed_document", return_value=embedding) as inference:
        segments = compressor.get_or_create_segments(chunk)
        assert segments
        initial_calls = inference.call_count
        compressor.get_or_create_segments(chunk)
        assert inference.call_count == initial_calls
        with patch("documents.services.embedding_runtime_config.load_embedding_runtime",
                   return_value=SimpleNamespace(generation_id="new", runtime_fingerprint="new")):
            refreshed = compressor.get_or_create_segments(chunk)
        assert inference.call_count > initial_calls
        assert refreshed[0].provenance["runtime_fingerprint"] == "new"
