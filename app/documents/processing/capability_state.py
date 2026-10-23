"""Finalize domain state when queued work loses capability admission."""

from django.utils import timezone

from config.enums import AIStatus


def finalize_capability_rejection(
    work_kind: str,
    payload: dict,
    *,
    error_code: str,
    message: str,
) -> None:
    error = {"code": error_code, "message": message}
    if work_kind == "parse_document":
        from documents.models import DocumentParseResult

        result, _created = DocumentParseResult.objects.get_or_create(
            node_id=payload["node_id"]
        )
        result.status = AIStatus.FAILED
        result.errors = [error]
        result.save(update_fields=["status", "errors", "updated_at"])
        return

    if work_kind in {"plan_document_embedding", "embed_document_batch"}:
        from documents.models import DocumentChunk

        chunks = DocumentChunk.objects.filter(
            status__in=[AIStatus.PENDING, AIStatus.PROCESSING]
        )
        if "chunk_ids" in payload:
            chunks = chunks.filter(pk__in=payload["chunk_ids"])
        else:
            chunks = chunks.filter(parse_result__node_id=payload["node_id"])
        chunks.update(status=AIStatus.FAILED, error_message=error)
        return

    if work_kind == "evaluate_retrieval":
        from documents.models import WorkspaceQualityEvaluationRun

        WorkspaceQualityEvaluationRun.objects.filter(uid=payload["run_uid"]).update(
            status=WorkspaceQualityEvaluationRun.STATUS_FAILED,
            error_message=message[:500],
            finished_at=timezone.now(),
        )
