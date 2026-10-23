"""Documents work handlers; loaded only by the owning executor role."""
from dataclasses import dataclass
from typing import Any

from documents.orchestration import WorkKind, derive_job_id
from orchestration.contracts import validate_followups
from documents.services.executor_jobs import finish_execution


def _target_key(work_kind: str, payload: dict[str, Any]) -> str:
    if work_kind in {WorkKind.PARSE_DOCUMENT.value, WorkKind.PLAN_DOCUMENT_EMBEDDING.value}:
        node_id = payload.get("node_id")
        if not isinstance(node_id, int) or node_id < 1:
            raise ValueError("payload.node_id must be a positive integer")
        return f"node:{node_id}"
    if work_kind == WorkKind.EMBED_DOCUMENT_BATCH.value:
        chunk_ids = payload.get("chunk_ids")
        if not isinstance(chunk_ids, list) or not chunk_ids or any(not isinstance(value, int) or value < 1 for value in chunk_ids):
            raise ValueError("payload.chunk_ids must be a non-empty list of positive integers")
        return "chunks:" + ",".join(str(value) for value in sorted(set(chunk_ids)))
    if work_kind == WorkKind.EVALUATE_RETRIEVAL.value:
        run_uid = payload.get("run_uid")
        if not isinstance(run_uid, str) or not run_uid:
            raise ValueError("payload.run_uid must be a non-empty string")
        return f"evaluation:{run_uid}"
    raise ValueError("Unsupported work_kind")


def _execute_parser(envelope: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from documents.processing.parsing import ParseExecutionFailed, execute_parse_node, get_parser_for_execution
    from capabilities.registry import DOCUMENT_SEMANTIC_SEARCH
    from documents.services.capability_admission import capability_enabled

    node_id = envelope["payload"]["node_id"]
    from documents.models import ExecutorJobReceipt
    followups = []
    if capability_enabled(DOCUMENT_SEMANTIC_SEARCH):
        followups = [{
            "job_id": derive_job_id(envelope["job_id"], WorkKind.PLAN_DOCUMENT_EMBEDDING, str(node_id)),
            "work_kind": WorkKind.PLAN_DOCUMENT_EMBEDDING.value,
            "payload": {"node_id": node_id}, "parent_job_id": envelope["job_id"],
        }]
    validate_followups(followups)
    def persisted(result):
        from documents.services.executor_snapshot import validate_execution
        validate_execution()
        receipt = ExecutorJobReceipt.objects.get(job_id=envelope["job_id"])
        finish_execution(receipt, result=result, followups=followups)
    try:
        result = execute_parse_node(
            node_id,
            parser=get_parser_for_execution(),
            on_persisted=persisted,
            trace_id=envelope.get("trace_id"),
            enqueued_at=envelope.get("enqueued_at"),
            job_id=envelope["job_id"],
            parent_job_id=envelope.get("parent_job_id"),
            work_kind=envelope["work_kind"],
            envelope_version=envelope["envelope_version"],
        )
    except ParseExecutionFailed as exc:
        raise exc.cause
    if result.get("status") != "success":
        return result, []
    return result, followups


def _execute_embedding(envelope: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    kind = envelope["work_kind"]
    payload = envelope["payload"]
    if kind == WorkKind.PLAN_DOCUMENT_EMBEDDING.value:
        from documents.embedding.executor import enqueue_embedding_tasks_sync

        batches: list[list[int]] = []
        result = enqueue_embedding_tasks_sync(
            payload["node_id"],
            trace_id=envelope.get("trace_id"),
            parent_job_id=envelope["job_id"],
            dispatch_batch=lambda chunk_ids, **_kwargs: batches.append(chunk_ids),
        )
        followups = [{
            "job_id": derive_job_id(envelope["job_id"], WorkKind.EMBED_DOCUMENT_BATCH, ",".join(map(str, batch))),
            "work_kind": WorkKind.EMBED_DOCUMENT_BATCH.value,
            "payload": {"chunk_ids": batch},
            "parent_job_id": envelope["job_id"],
        } for batch in batches]
        validate_followups(followups)
        return result, followups
    if kind == WorkKind.EMBED_DOCUMENT_BATCH.value:
        from documents.embedding.executor import embed_document_chunks_batch_sync

        return embed_document_chunks_batch_sync(
            payload["chunk_ids"],
            trace_id=envelope.get("trace_id"),
            enqueued_at=envelope.get("enqueued_at"),
            job_id=envelope["job_id"],
            parent_job_id=envelope.get("parent_job_id"),
            work_kind=kind,
            envelope_version=envelope["envelope_version"],
        ), []
    if kind == WorkKind.EVALUATE_RETRIEVAL.value:
        from documents.search.evaluation import execute_quality_evaluation_run

        execute_quality_evaluation_run(payload["run_uid"])
        from documents.models import WorkspaceQualityEvaluationRun
        run = WorkspaceQualityEvaluationRun.objects.get(uid=payload["run_uid"])
        return {"status": "success" if run.status == "succeeded" else "failed",
                "run_uid": payload["run_uid"], "error": run.error_message}, []
    raise ValueError("Unsupported embedding work_kind")



@dataclass(frozen=True)
class DocumentWorkHandler:
    kind: str
    atomic_execution: bool = False

    def target_key(self, payload):
        return _target_key(self.kind, payload)

    def capture_snapshot(self, envelope):
        from documents.services.executor_snapshot import capture_snapshot
        return capture_snapshot(envelope)

    def execute(self, envelope):
        if self.kind == WorkKind.PARSE_DOCUMENT.value:
            return _execute_parser(envelope)
        return _execute_embedding(envelope)


@dataclass(frozen=True)
class ScopedEmbeddingHandler:
    kind: str = "embed_document"
    atomic_execution: bool = False
    manages_receipt: bool = True

    def target_key(self, payload):
        from documents.processing.embedding_contract import parse_embedding_delivery
        return "manifest:" + parse_embedding_delivery(payload).manifest_id

    def capture_snapshot(self, envelope):
        from documents.services.executor_snapshot import capture_snapshot
        return capture_snapshot(envelope)

    def execute(self, envelope):
        from documents.embedding.completion import execute_scoped_receipted
        receipt = execute_scoped_receipted(envelope)
        return receipt.result, receipt.followups


PARSE_HANDLER = DocumentWorkHandler("parse_document")
PLAN_EMBEDDING_HANDLER = DocumentWorkHandler("plan_document_embedding", atomic_execution=True)
EMBED_BATCH_HANDLER = DocumentWorkHandler("embed_document_batch")
EVALUATION_HANDLER = DocumentWorkHandler("evaluate_retrieval")
SCOPED_EMBEDDING_HANDLER = ScopedEmbeddingHandler()
