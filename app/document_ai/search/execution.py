from __future__ import annotations

import json
import logging
from time import perf_counter

from django.utils import timezone

from config.enums import AIStatus, RAGStage
from document_ai.performance import datetime_delta_ms, elapsed_ms, put_metric

logger = logging.getLogger(__name__)


def _queue_rag_generation_for_search_job(search_job) -> int:
    from document_ai.models import RAGJob
    from document_ai.tasks import generate_rag_response

    queued = 0
    rag_jobs = RAGJob.objects.filter(
        search_job=search_job,
        status=AIStatus.PENDING,
        task_id="",
    )
    for rag_job in rag_jobs:
        async_result = generate_rag_response.apply_async(args=[rag_job.id], queue="rag")
        rag_job.task_id = async_result.id
        rag_job.stage = RAGStage.GENERATING
        rag_job.stage_message = "검색된 근거를 바탕으로 답변을 생성하고 있습니다."
        rag_job.save(update_fields=["task_id", "stage", "stage_message", "updated_at"])
        queued += 1
    return queued


def _fail_rag_jobs_for_search_job(search_job, message: str) -> int:
    from document_ai.models import RAGJob

    return RAGJob.objects.filter(
        search_job=search_job,
        status=AIStatus.PENDING,
    ).update(
        status=AIStatus.FAILED,
        stage=RAGStage.FAILED,
        stage_message="근거 검색에 실패했습니다.",
        completed_at=search_job.completed_at or timezone.now(),
        error_message=message,
    )


def _get_search_job_orm_constraints(search_job) -> dict:
    query_log = getattr(search_job, "query_log", None)
    if not query_log:
        return {}
    orm = query_log.orm or {}
    return {
        "filter_kwargs": orm.get("filter_kwargs") or {},
        "exclude_kwargs": orm.get("exclude_kwargs") or {},
        "order_by": orm.get("order_by") or [],
    }


def perform_vector_search_sync(job_id: int, *, retries: int = 0, max_retries: int = 1) -> dict:
    from document_ai.models import SearchJob
    from document_ai.search.retriever import VectorRetriever

    try:
        job = SearchJob.objects.select_related("owner", "query_log").get(pk=job_id)
    except SearchJob.DoesNotExist:
        logger.error("Search job %s not found", job_id)
        return {
            "status": "failed",
            "job_id": job_id,
            "error": f"Search job {job_id} not found",
        }

    worker_started = perf_counter()
    job.status = AIStatus.PROCESSING
    job.started_at = timezone.now()
    job.error_message = ""
    metrics = dict(job.performance_metrics or {})
    if "queue_wait_ms" not in metrics:
        put_metric(metrics, "queue_wait_ms", datetime_delta_ms(job.created_at, job.started_at))
    metrics["attempt"] = retries + 1
    job.performance_metrics = metrics
    job.save(update_fields=["status", "started_at", "error_message", "performance_metrics"])

    try:
        retriever = VectorRetriever()
        logger.info(
            "Vector search started: job_id=%s, owner_id=%s, top_k=%s, tuning_params=%s",
            job.id,
            job.owner_id,
            job.top_k,
            job.tuning_params,
        )
        retrieval_metrics = {}
        results = retriever.retrieve(
            query=job.query,
            top_k=job.top_k,
            threshold=job.threshold,
            node_ids=job.node_ids or None,
            user=job.owner,
            tuning_params=job.tuning_params,
            performance_metrics=retrieval_metrics,
            **_get_search_job_orm_constraints(job),
        )

        normalized_results = json.loads(json.dumps(results, default=str))

        job.results = normalized_results
        job.status = AIStatus.COMPLETED
        job.completed_at = timezone.now()
        job.error_message = ""
        metrics.update(retrieval_metrics)
        put_metric(metrics, "worker_total_ms", elapsed_ms(worker_started))
        put_metric(metrics, "end_to_end_ms", datetime_delta_ms(job.created_at, job.completed_at))
        put_metric(metrics, "result_count", len(normalized_results))
        job.performance_metrics = metrics
        job.save(update_fields=["results", "status", "completed_at", "error_message", "performance_metrics"])

        logger.info(
            "Vector search completed: job_id=%s, result_count=%s, performance=%s",
            job.id,
            len(normalized_results),
            metrics,
        )
        queued_rag_jobs = _queue_rag_generation_for_search_job(job)
        if queued_rag_jobs:
            logger.info(
                "Queued RAG generation after search completion: search_job_id=%s rag_jobs=%s",
                job.id,
                queued_rag_jobs,
            )

        return {
            "status": "success",
            "job_id": job_id,
            "result_count": len(normalized_results),
            "queued_rag_jobs": queued_rag_jobs,
        }

    except Exception as exc:
        if retries < max_retries:
            raise

        job.status = AIStatus.FAILED
        job.completed_at = timezone.now()
        job.error_message = str(exc)
        put_metric(metrics, "worker_total_ms", elapsed_ms(worker_started))
        put_metric(metrics, "end_to_end_ms", datetime_delta_ms(job.created_at, job.completed_at))
        metrics["failed"] = True
        job.performance_metrics = metrics
        job.save(update_fields=["status", "completed_at", "error_message", "performance_metrics"])
        _fail_rag_jobs_for_search_job(job, job.error_message or "Search failed.")

        logger.exception("Vector search failed: job_id=%s", job_id)
        return {
            "status": "failed",
            "job_id": job_id,
            "error": str(exc),
        }
