from __future__ import annotations

import logging

from celery import shared_task
from capabilities.admission import CapabilityAdmissionError
from orchestration.celery_adapter import (
    _DISPATCH_SLOTS, executor_envelope, publish_executor_followups, dispatch_or_retry,
)

from documents.orchestration import (
    WorkKind,
    embedding_queue_backpressure as _embedding_queue_backpressure,
    enqueue_embedding_batch,
    enqueue_embedding,
    enqueue_parse,
    enqueue_retrieval_evaluation,
    enqueue_registered_work,
    get_work_capability,
)
from documents.services.environment import get_positive_int_env as _get_positive_int_env

from config.tracing import get_trace_id, new_trace_id, set_trace_id

logger = logging.getLogger(__name__)


@shared_task(name="documents.tasks.embed_document_scoped", queue="embed", bind=True,
             acks_late=True, reject_on_worker_lost=True)
def embed_document_scoped(self, schema, command, manifest, **identity):
    envelope = executor_envelope(**identity)
    return dispatch_or_retry(self, "embed_document", envelope=envelope,
        payload={"schema": schema, "command": command, "manifest": manifest})


# Compatibility names for extensions/tests that historically imported these
# helpers from tasks.py. Their real ownership remains documents.rag and they
# are lazy so a queue-only orchestrator does not import the RAG stack at boot.
def _build_rag_context(*args, **kwargs):
    from documents.rag.generation import _build_rag_context as implementation

    return implementation(*args, **kwargs)


def _extract_llm_final_content(*args, **kwargs):
    from documents.rag.generation import _extract_llm_final_content as implementation

    return implementation(*args, **kwargs)


def _executor_envelope(**kwargs):
    return executor_envelope(**kwargs)


def _publish_executor_followups(response: dict) -> None:
    return publish_executor_followups(response, publish=enqueue_registered_work)


def _dispatch_or_retry(task, kind: WorkKind, *, envelope: dict, payload: dict) -> dict:
    from documents.processing.capability_state import finalize_capability_rejection
    from documents.services.executor_transport import dispatch_executor_job

    return dispatch_or_retry(
        task, kind, envelope=envelope, payload=payload,
        on_rejected=finalize_capability_rejection,
        dispatch=dispatch_executor_job, publish_followups=_publish_executor_followups,
    )


# Task names are pinned explicitly: queued messages, WORK_REGISTRY.task_name
# and persisted deployment_plan.json work_routes all reference these exact
# strings, so renaming the module must not silently rename the task.
@shared_task(name="documents.tasks.recover_document_pipeline_backlog", queue="parse")
def recover_document_pipeline_backlog() -> dict:
    from documents.processing.recovery import recover_document_pipeline_backlog_sync

    return recover_document_pipeline_backlog_sync()


@shared_task(
    name="documents.tasks.parse_document_with_docling",
    queue="parse",
    bind=True,
    max_retries=2,
    retry_backoff=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def parse_document_with_docling(
    self,
    node_id: int,
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
) -> dict:
    """
    Celery 태스크: 파싱 → DB 저장 오케스트레이션
    node_id를 받아 파일 경로를 조회하고, 파싱 후 결과를 DB에 저장
    """
    should_pause, embed_queue_depth, backpressure_limit = _embedding_queue_backpressure()
    if should_pause:
        retry_seconds = _get_positive_int_env(
            "DOCUMENT_AI_PARSE_BACKPRESSURE_RETRY_SECONDS", 5
        )
        logger.warning(
            "Parse deferred by embed queue backpressure: node_id=%s, embed_queue_depth=%s, limit=%s, retry_seconds=%s",
            node_id,
            embed_queue_depth,
            backpressure_limit,
            retry_seconds,
        )
        raise self.retry(
            exc=RuntimeError(
                f"embed queue depth {embed_queue_depth} reached limit {backpressure_limit}"
            ),
            countdown=retry_seconds,
            max_retries=None,
        )

    return _dispatch_or_retry(
        self,
        WorkKind.PARSE_DOCUMENT,
        envelope=_executor_envelope(
            job_id=job_id,
            parent_job_id=parent_job_id,
            trace_id=trace_id,
            enqueued_at=enqueued_at,
            work_kind=work_kind,
            envelope_version=envelope_version,
        ),
        payload={"node_id": node_id},
    )


@shared_task(name="documents.tasks.run_quality_evaluation_task", queue="embed", bind=True, acks_late=True, reject_on_worker_lost=True)
def run_quality_evaluation_task(
    self,
    run_uid: str,
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
) -> dict:
    """Runs a workspace quality-profile evaluation (WorkspaceQualityEvaluationRun)
    and persists its metrics. Defined here rather than in documents.search.evaluation
    because Celery's autodiscover_tasks() only imports each app's tasks.py module."""
    return _dispatch_or_retry(
        self,
        WorkKind.EVALUATE_RETRIEVAL,
        envelope=_executor_envelope(
            job_id=job_id,
            parent_job_id=parent_job_id,
            trace_id=trace_id,
            enqueued_at=enqueued_at,
            work_kind=work_kind,
            envelope_version=envelope_version,
        ),
        payload={"run_uid": run_uid},
    )


@shared_task(
    name="documents.tasks.enqueue_embedding_tasks",
    queue="embed",
    bind=True,
    max_retries=3,
    retry_backoff=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def enqueue_embedding_tasks(
    self,
    node_id: int,
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
) -> dict:
    """
    Celery 태스크: 임베딩 → DB 저장 오케스트레이션
    node_id를 받아 파일 경로를 조회하고, 임베딩 후 결과를 DB에 저장
    """
    return _dispatch_or_retry(
        self,
        WorkKind.PLAN_DOCUMENT_EMBEDDING,
        envelope=_executor_envelope(
            job_id=job_id,
            parent_job_id=parent_job_id,
            trace_id=trace_id,
            enqueued_at=enqueued_at,
            work_kind=work_kind,
            envelope_version=envelope_version,
        ),
        payload={"node_id": node_id},
    )


@shared_task(
    name="documents.tasks.embedding_document_with_bge",
    queue="embed",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def embedding_document_with_bge(
    self, chunk_id: int, trace_id: str | None = None, enqueued_at: str | None = None
) -> dict:
    """
    Celery 태스크: 임베딩 → DB 저장 오케스트레이션
    node_id를 받아 파일 경로를 조회하고, 임베딩 후 결과를 DB에 저장
    """
    from documents.orchestration import TaskEnvelope

    envelope = TaskEnvelope.create(WorkKind.EMBED_DOCUMENT_BATCH, job_id=self.request.id, trace_id=trace_id, enqueued_at=enqueued_at)
    return _dispatch_or_retry(
        self,
        WorkKind.EMBED_DOCUMENT_BATCH,
        envelope=envelope.as_task_kwargs(),
        payload={"chunk_ids": [chunk_id]},
    )


@shared_task(
    name="documents.tasks.embedding_document_batch_with_bge",
    queue="embed",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def embedding_document_batch_with_bge(
    self,
    chunk_ids: list[int],
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
) -> dict:
    """
    Celery 태스크: 같은 문서의 청크 묶음을 model.encode() 한 번으로 임베딩.
    enqueue_embedding_tasks_sync가 청크 수/토큰 예산으로 미리 묶어 넘긴 chunk_ids를
    받아 처리한다. 실패 시 배치 전체를 하나의 단위로 재시도한다.
    """
    return _dispatch_or_retry(
        self,
        WorkKind.EMBED_DOCUMENT_BATCH,
        envelope=_executor_envelope(
            job_id=job_id,
            parent_job_id=parent_job_id,
            trace_id=trace_id,
            enqueued_at=enqueued_at,
            work_kind=work_kind,
            envelope_version=envelope_version,
        ),
        payload={"chunk_ids": chunk_ids},
    )


@shared_task(name="documents.tasks.parse_user_query", queue="query")
def parse_user_query(
    query: str,
    mode: str = "search",
    trace_id: str | None = None,
    enqueued_at: str | None = None,
    job_id: str | None = None,
    parent_job_id: str | None = None,
    work_kind: str | None = None,
    envelope_version: int | None = None,
) -> dict:
    """
    사용자 질의를 LLM 기반 QueryDSL 후보로 파싱하고 query_engine에서 검증/ORM 컴파일합니다.
    규칙 기반 QueryAnalyzer는 사용하지 않으며, 실패 시 원 질의를 semantic query로 보존합니다.
    """
    from documents.query_understanding.parser_service import parse_user_query_sync

    _ = (enqueued_at, job_id, parent_job_id, work_kind, envelope_version)
    set_trace_id(trace_id or new_trace_id())
    return parse_user_query_sync(query, mode)
