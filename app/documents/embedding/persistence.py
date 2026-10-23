"""Batch-local input evidence and locked persistence guards, not planning."""
import hashlib
from contextlib import contextmanager

from django.db import transaction
from orchestration.execution import StaleExecution
from orchestration.receipts import active_receipt_ownership, ExecutionOwnershipLost


def input_evidence(chunk, *, blob=None, runtime=None):
    result = chunk.parse_result
    from documents.processing.parse_artifacts import parse_content_fingerprint
    from files.models import FileBlob
    if blob is None:
        blob = FileBlob.objects.filter(node_id=result.node_id).first()
    if runtime is None:
        from documents.services.embedding_runtime_config import load_embedding_runtime
        runtime = load_embedding_runtime()
    return {
        "parse_status": result.status,
        "parse_revision": str(result.parse_revision or ""),
        "processing_fingerprint": (result.metadata or {}).get("processing_fingerprint", ""),
        "content_fingerprint": hashlib.sha256((chunk.text or "").encode()).hexdigest(),
        "target_fingerprint": parse_content_fingerprint([{"id": chunk.pk,
            "chunk_index": chunk.chunk_index, "text": chunk.text, "chunk_meta": chunk.chunk_meta}]),
        "source_revision": str(blob.uuid) if blob else "",
        "source_checksum": (blob.sha256 or "") if blob else "",
        "generation_id": runtime.generation_id,
        "runtime_fingerprint": runtime.runtime_fingerprint,
    }


def capture_batch_evidence(chunks):
    from files.models import FileBlob
    from documents.services.embedding_runtime_config import load_embedding_runtime
    blobs = {blob.node_id: blob for blob in FileBlob.objects.filter(
        node_id__in={chunk.parse_result.node_id for chunk in chunks})}
    runtime = load_embedding_runtime()
    return {chunk.pk: input_evidence(chunk, blob=blobs.get(chunk.parse_result.node_id), runtime=runtime)
            for chunk in chunks}


@contextmanager
def guarded_embedding_write(chunks, expected):
    from documents.models import DocumentChunk, DocumentParseResult, ExecutorJobReceipt
    from files.models import Node, FileBlob
    from documents.services.executor_snapshot import validate_execution
    with transaction.atomic():
        owner = active_receipt_ownership.get()
        if owner is not None:
            current = ExecutorJobReceipt.objects.select_for_update().get(pk=owner.job_id)
            owner.validate(current)
            if current.status != "running":
                raise ExecutionOwnershipLost("Embedding writer no longer owns a running receipt")
        node_ids = sorted({chunk.parse_result.node_id for chunk in chunks})
        # Match manifest approval's source -> blob -> parse lock order.
        list(Node.objects.select_for_update().filter(pk__in=node_ids).order_by("pk"))
        list(FileBlob.objects.select_for_update().filter(node_id__in=node_ids).order_by("node_id"))
        list(DocumentParseResult.objects.select_for_update().filter(node_id__in=node_ids).order_by("pk"))
        current_chunks = list(DocumentChunk.objects.select_related("parse_result__node").select_for_update(of=("self",)).filter(
            pk__in=[chunk.pk for chunk in chunks]).order_by("pk"))
        if len(current_chunks) != len(chunks):
            raise StaleExecution("Embedding chunks were replaced")
        current_evidence = capture_batch_evidence(current_chunks)
        for current in current_chunks:
            metadata = current.parse_result.metadata or {}
            evidence = current_evidence[current.pk]
            if (not current.parse_result.node.ai_processing_enabled
                    or current.parse_result.node.trashed
                    or current.parse_result.status != "completed"
                    or evidence != expected[current.pk]
                    or (metadata.get("source_blob_uuid") and metadata["source_blob_uuid"] != evidence["source_revision"])
                    or (metadata.get("source_checksum") and metadata["source_checksum"] != evidence["source_checksum"])):
                raise StaleExecution("Embedding input changed before persistence")
        validate_execution()
        yield
        validate_execution()
        from documents.services.embedding_runtime_config import load_embedding_runtime
        runtime = load_embedding_runtime()
        if any(value["generation_id"] != runtime.generation_id
               or value["runtime_fingerprint"] != runtime.runtime_fingerprint
               for value in expected.values()):
            raise StaleExecution("Embedding runtime changed before commit")


def serving_provenance(chunk, store):
    evidence = getattr(chunk, "_embedding_input_evidence", None)
    if evidence is None:
        return None  # Compatibility for explicit legacy/direct store callers.
    owner = active_receipt_ownership.get()
    if (evidence["generation_id"] != store.spec.generation_id
            or evidence["runtime_fingerprint"] != store.runtime_fingerprint):
        raise StaleExecution("Embedding store differs from captured runtime")
    return {"schema": "dotori.embedding-provenance.v1", **evidence,
            "generation_id": store.spec.generation_id,
            "runtime_fingerprint": store.runtime_fingerprint,
            "job_id": owner.job_id if owner else "", "attempt": owner.attempt if owner else None}


def valid_serving_rows(queryset, store):
    """Legacy rows retain document markers; stamped rows use their own evidence."""
    from django.db.models import F, Q, CharField, Func
    from django.db.models.functions import Cast, Coalesce
    from django.db.models import Value
    legacy = Q(provenance__isnull=True,
               chunk__status="completed",
               chunk__parse_result__embedding_generation_id=store.spec.generation_id,
               chunk__parse_result__embedding_runtime_fingerprint=store.runtime_fingerprint)
    stamped = Q(provenance__schema="dotori.embedding-provenance.v1",
                chunk__parse_result__status="completed",
                provenance__generation_id=store.spec.generation_id,
                provenance__runtime_fingerprint=store.runtime_fingerprint)
    queryset = queryset.annotate(
        _parse_revision=Coalesce(Cast("chunk__parse_result__parse_revision", CharField()), Value("")),
        _content_fingerprint=Func(F("chunk__text"),
            template="encode(sha256(convert_to(%(expressions)s, 'UTF8')), 'hex')",
            output_field=CharField()),
        _source_revision=Coalesce(Cast("chunk__parse_result__node__blob__uuid", CharField()), Value("")),
        _source_checksum=Coalesce("chunk__parse_result__node__blob__sha256", Value("")))
    # JSON text transforms avoid comparing JSON scalars to SQL text values.
    from django.db.models.fields.json import KeyTextTransform
    for key, expression in {
        "parse_revision": F("_parse_revision"), "content_fingerprint": F("_content_fingerprint"),
        "source_revision": F("_source_revision"), "source_checksum": F("_source_checksum"),
        "processing_fingerprint": Coalesce(KeyTextTransform("processing_fingerprint", "chunk__parse_result__metadata"), Value("")),
    }.items():
        alias = "_stamp_" + key
        queryset = queryset.annotate(**{alias: KeyTextTransform(key, "provenance")})
        stamped &= Q(**{alias: expression})
    return queryset.filter(legacy | stamped)
