"""Internal scoped execution port. No queue registration or HTTP activation.

The authenticated delivery adapter must supply the common receipt owner.
This port never claims work, changes plan mode, reparses or chooses targets.
"""
from documents.models import DocumentChunk, ExecutorJobReceipt
from documents.embedding.executor import (
    _group_chunks_into_batches, execute_embedding_microbatch, prepare_embedding_input_text,
)
from documents.embedding.persistence import capture_batch_evidence, guarded_embedding_write
from documents.embedding.store_registry import get_embedding_store_instance
from documents.parsers.text_utils import normalize_extracted_text
from documents.processing.embedding_batches import bind_embedding_batch
from documents.processing.embedding_contract import (
    DOCUMENT_EMBED_WORK_KIND, TargetOutcome, build_embedding_result, parse_embedding_delivery,
)
from documents.processing.embedding_manifests import validate_execution_manifest
from orchestration.execution import StaleExecution
from orchestration.receipts import active_receipt_ownership, canonical_fingerprint, ExecutionOwnershipLost


def execute_scoped_embedding(envelope, *, on_completed=None):
    """Execute the whole fixed manifest under one already-claimed receipt.

    Transient inference errors propagate to the delivery retry classifier.
    No durable batch checkpoint or per-target progress rows are created.
    """
    delivery = parse_embedding_delivery(envelope["payload"])
    owner = active_receipt_ownership.get()
    if (owner is None or owner.job_id != envelope["job_id"]
            or owner.work_kind != DOCUMENT_EMBED_WORK_KIND
            or envelope.get("work_kind") != DOCUMENT_EMBED_WORK_KIND
            or owner.payload_fingerprint != canonical_fingerprint(envelope["payload"])):
        raise ExecutionOwnershipLost("Scoped execution requires its authenticated receipt owner")
    receipt = ExecutorJobReceipt.objects.get(pk=owner.job_id)
    owner.validate(receipt)
    if receipt.status == "succeeded":
        return receipt.result  # Delivery redelivery, not fresh domain readiness evidence.
    if receipt.status != "running":
        raise ExecutionOwnershipLost("Scoped execution requires a running receipt")

    def validate_approval():
        return validate_execution_manifest(manifest_id=delivery.manifest_id,
            fingerprint=delivery.manifest_fingerprint, plan_id=envelope["plan_id"],
            step_id=envelope["step_id"], job_id=owner.job_id, command=delivery.command)

    approved = validate_approval()
    chunks = list(DocumentChunk.objects.select_related("parse_result__node")
        .filter(pk__in=approved).order_by("chunk_index", "pk"))
    if {chunk.pk for chunk in chunks} != set(approved):
        raise StaleExecution("Manifest chunks disappeared")
    evidence = capture_batch_evidence(chunks)
    command = delivery.command
    for chunk in chunks:
        current = evidence[chunk.pk]
        if (current["parse_revision"] != command.input.revision
                or current["processing_fingerprint"] != command.input.processing_fingerprint
                or current["source_revision"] != command.source.revision
                or current["source_checksum"] != command.source.checksum
                or current["generation_id"] != command.runtime.generation_id
                or current["runtime_fingerprint"] != command.runtime.fingerprint):
            raise StaleExecution("Manifest input changed before inference")
        chunk._embedding_input_evidence = current

    store = get_embedding_store_instance()
    if (store.spec.generation_id != command.runtime.generation_id
            or store.runtime_fingerprint != command.runtime.fingerprint):
        raise StaleExecution("Selected embedding store differs from the approved runtime")
    existing = {row.chunk_id: row for row in store.base_queryset().filter(chunk_id__in=approved)}
    reused = {}
    if command.mode == "missing":
        reused = {chunk_id: row.provenance for chunk_id, row in existing.items()
                  if not row.provenance or row.provenance.get("job_id") != owner.job_id}
    outcomes = {chunk_id: TargetOutcome(chunk_id, "reused", f"chunk-embedding:{chunk_id}")
                for chunk_id in reused}
    selected = [chunk for chunk in chunks if chunk.pk not in reused]
    by_id = {chunk.pk: chunk for chunk in selected}
    batches = _group_chunks_into_batches([(chunk.pk, chunk.token_count) for chunk in selected])
    for ids in batches:
        current_receipt = ExecutorJobReceipt.objects.get(pk=owner.job_id)
        owner.validate(current_receipt)
        if current_receipt.status != "running":
            raise ExecutionOwnershipLost("Scoped batch no longer owns a running receipt")
        validate_approval()  # Revalidate revocation/input before the next model call.
        binding = bind_embedding_batch(parent_job_id=owner.job_id,
            manifest_id=delivery.manifest_id, manifest_fingerprint=delivery.manifest_fingerprint,
            target_ids=tuple(ids), approved_target_ids=approved)
        valid_chunks, texts = [], []
        for chunk_id in ids:
            chunk = by_id[chunk_id]
            text = normalize_extracted_text(chunk.text or "")
            if not text:
                outcomes[chunk_id] = TargetOutcome(chunk_id, "failed")
                continue
            text, _, _ = prepare_embedding_input_text(text, chunk=chunk)
            valid_chunks.append(chunk)
            texts.append(text)
        if not valid_chunks:
            continue
        def metrics(chunk):
            return {**(chunk.performance_metrics or {}), "job_id": owner.job_id,
                    "attempt": owner.attempt, "manifest_id": delivery.manifest_id,
                    "batch_id": binding.batch_id, "work_kind": DOCUMENT_EMBED_WORK_KIND}
        succeeded = execute_embedding_microbatch(chunks=valid_chunks, texts=texts,
            evidence={chunk.pk: evidence[chunk.pk] for chunk in valid_chunks},
            store=store, build_metrics=metrics, validate_approval=validate_approval)
        for chunk_id in succeeded:
            outcomes[chunk_id] = TargetOutcome(chunk_id, "succeeded",
                f"chunk-embedding:{chunk_id}", owner.job_id)

    # Completion is based on current domain evidence, not just successful calls.
    with guarded_embedding_write(chunks, evidence):
        validate_approval()
        serving = {row.chunk_id: row for row in store.base_queryset().filter(chunk_id__in=approved)}
        for chunk_id, outcome in outcomes.items():
            if outcome.status == "failed":
                continue
            row = serving.get(chunk_id)
            if row is None:
                raise StaleExecution("Completion vector is no longer valid")
            stamp = row.provenance or {}
            if outcome.status == "succeeded":
                if stamp.get("job_id") != owner.job_id or stamp.get("attempt") != owner.attempt:
                    raise StaleExecution("Completion belongs to a different receipt attempt")
            elif row.provenance != reused[chunk_id]:
                raise StaleExecution("Reused completion evidence changed")
        result = build_embedding_result(command, job_id=owner.job_id,
            manifest_id=delivery.manifest_id, manifest_fingerprint=delivery.manifest_fingerprint,
            target_ids=approved, outcomes=tuple(outcomes.values()))
        if on_completed is not None:
            return on_completed(result)
    return result
