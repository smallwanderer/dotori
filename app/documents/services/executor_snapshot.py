"""Small input identity snapshots; no document bodies or vectors."""
from documents.models import DocumentChunk
from documents.services.embedding_runtime_config import load_embedding_runtime
from files.models import FileBlob
from orchestration.execution import active_execution, StaleExecution
from orchestration.execution import validate_execution as _validate_execution


def validate_execution():
    # Preserve the old patch boundary and shared ContextVar/exception identity.
    return _validate_execution(capture_snapshot)


def capture_snapshot(envelope):
    from capabilities.deployment import load_deployment_snapshot
    from capabilities.registry import DOCUMENT_SEMANTIC_SEARCH

    payload = envelope["payload"]
    node_ids = [payload["node_id"]] if "node_id" in payload else []
    if envelope.get("work_kind") == "embed_document":
        from documents.models import DocumentEmbeddingManifest
        from documents.processing.embedding_contract import parse_embedding_delivery
        delivery = parse_embedding_delivery(payload)
        if not envelope.get("plan_id") or not envelope.get("step_id"):
            raise ValueError("Scoped embedding requires an approved plan and step")
        try:
            manifest = DocumentEmbeddingManifest.objects.get(pk=delivery.manifest_id,
                job_id=envelope["job_id"], plan_id=envelope["plan_id"], step_id=envelope["step_id"])
        except DocumentEmbeddingManifest.DoesNotExist as exc:
            raise ValueError("Scoped embedding manifest is not approved") from exc
        node_ids = [manifest.node_id]
    if "chunk_ids" in payload:
        node_ids = list(DocumentChunk.objects.filter(pk__in=payload["chunk_ids"])
                        .order_by().values_list("parse_result__node_id", flat=True).distinct())
    blobs = list(FileBlob.objects.filter(node_id__in=node_ids).order_by("node_id")
                 .values("node_id", "uuid", "file", "size"))
    for blob in blobs:
        blob["uuid"] = str(blob["uuid"])
    deployment = load_deployment_snapshot()
    if (
        envelope.get("work_kind") == "parse_document"
        and not deployment.is_enabled(DOCUMENT_SEMANTIC_SEARCH)
    ):
        runtime = {
            "capability_revision": deployment.capability_revision,
            "chunking_strategy": "hierarchical",
        }
    else:
        runtime = {"fingerprint": load_embedding_runtime().runtime_fingerprint}
    return (
        {"payload": payload, "node_ids": sorted(node_ids), "blobs": blobs},
        runtime,
    )
