from __future__ import annotations

import logging
import json
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import UUID

from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q

from document_ai.db_span import capture_db_spans
from document_ai.embedding.store_registry import get_embedding_store_instance
from document_ai.models import DocumentParseResult, DocumentChunk
from document_ai.parsers.config import (
    get_chunk_max_tokens,
    get_embedding_backend,
    get_embedding_max_tokens,
    get_embedding_model,
    get_parser_tokenizer_id,
)
from document_ai.parsers.text_utils import normalize_extracted_text, serialize_meta
from document_ai.performance import datetime_delta_ms, elapsed_ms, put_metric
from document_ai.tracing_utils import enqueue_kwargs

from config.enums import AIStatus
from config.tracing import get_trace_id, new_trace_id, set_trace_id
from document_ai.rag.generation import _build_rag_context

if TYPE_CHECKING:
    from document_ai.parsers.schema import ParseResult

logger = logging.getLogger(__name__)


def _json_safe(value):
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _get_llm_message_content(payload: dict) -> str:
    choice = (payload.get("choices") or [{}])[0]
    message_content = choice.get("message", {}).get("content", "")
    if isinstance(message_content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in message_content
        ).strip()
    return str(message_content or choice.get("text", "") or "").strip()


def _get_responses_stream_delta_content(payload: dict) -> str:
    event_type = payload.get("type")
    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        return str(payload.get("delta") or "")

    if event_type == "response.output_item.done":
        item = payload.get("item") or {}
        content_parts = item.get("content") or []
        return "".join(
            str(part.get("text", ""))
            for part in content_parts
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}
        )

    response = payload.get("response") or {}
    output_parts = response.get("output") or payload.get("output") or []
    text_parts = []
    for item in output_parts:
        if not isinstance(item, dict):
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                text_parts.append(str(part.get("text", "")))
    if text_parts:
        return "".join(text_parts)

    return ""


def _get_chat_completions_stream_delta_content(payload: dict) -> str:
    choice = (payload.get("choices") or [{}])[0]
    delta = choice.get("delta") or {}
    content = delta.get("content")
    if content is None:
        content = choice.get("message", {}).get("content")
    if content is None:
        content = choice.get("text")
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content or "")


def _iter_openai_responses_stream_content(response):
    for raw_line in response.iter_lines(decode_unicode=False):
        if not raw_line:
            continue
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else str(raw_line)
        line = line.strip()
        if not line:
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON LLM stream line: %r", line[:200])
            continue
        content = _get_responses_stream_delta_content(payload)
        if not content:
            content = _get_chat_completions_stream_delta_content(payload)
        if content:
            yield content


def _strip_llm_control_tokens(text: str) -> str:
    cleaned = re.sub(r"<\|[^>]+?\|>", "", text or "").strip()
    cleaned = re.sub(r"^```(?:[a-zA-Z]+)?\s*|\s*```$", "", cleaned).strip()
    return cleaned


def _extract_llm_final_content(text: str, *, fallback_to_raw: bool = True) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""

    final_markers = [
        "<|channel>final",
        "<channel|>",
        "<|final|>",
        "</think>",
        "Final Output:",
        "Final Output",
        "최종 답변:",
        "답변:",
    ]
    for marker in final_markers:
        if marker in raw:
            return _strip_llm_control_tokens(raw.split(marker, 1)[1])

    if "<|channel>thought" in raw or "<|think" in raw or "<think>" in raw:
        return ""

    if not fallback_to_raw:
        return ""
    raw = re.sub(r"<\|channel\>thought[\s\S]*?(?=<\|channel\>final|$)", "", raw).strip()
    raw = re.sub(r"<think>[\s\S]*?(?=</think>|$)", "", raw).strip()
    return _strip_llm_control_tokens(raw)


def _get_positive_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default))
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, falling back to %s", name, raw_value, default)
        return default

    if value < 1:
        logger.warning("%s must be >= 1, falling back to %s", name, default)
        return default

    return value


def _get_query_parser_base_url() -> str:
    return os.getenv(
        "QUERY_UNDERSTANDING_PARSER_BASE_URL",
        os.getenv("QUERY_UNDERSTANDING_LLM_URL", "http://vllm-query-parser:8080"),
    ).rstrip("/")


def _get_query_parser_model() -> str:
    return os.getenv(
        "QUERY_UNDERSTANDING_PARSER_REQUEST_MODEL",
        os.getenv(
            "QUERY_UNDERSTANDING_PARSER_SERVED_MODEL_NAME",
            os.getenv(
                "QUERY_UNDERSTANDING_PARSER_MODEL",
                os.getenv("QUERY_UNDERSTANDING_LLM_MODEL", "meta-llama/Llama-3.2-3B-Instruct"),
            ),
        ),
    )


def _get_recovery_stale_minutes() -> int:
    return _get_positive_int_env("DOCUMENT_AI_RECOVERY_STALE_MINUTES", 30)


def _get_recovery_parse_batch_size() -> int:
    return _get_positive_int_env("DOCUMENT_AI_RECOVERY_PARSE_BATCH_SIZE", 50)


def _get_recovery_embedding_batch_size() -> int:
    return _get_positive_int_env("DOCUMENT_AI_RECOVERY_EMBED_BATCH_SIZE", 200)


def _get_max_recovery_attempts() -> int:
    return _get_positive_int_env("DOCUMENT_AI_MAX_RECOVERY_ATTEMPTS", 5)


def _recovery_cutoff():
    return timezone.now() - timedelta(minutes=_get_recovery_stale_minutes())


def _get_parse_recovery_node_ids(limit: int) -> list[int]:
    from files.models import Node

    cutoff = _recovery_cutoff()
    max_attempts = _get_max_recovery_attempts()

    # parse_result 가 없는 신규 파일은 attempt 제한 없이 항상 포함
    # parse_result 가 있는 파일은 staleness + attempt 한계 조건 모두 적용
    node_ids = set(
        Node.objects.select_related("blob", "parse_result")
        .filter(
            node_type="file",
            trashed=False,
            ai_processing_enabled=True,
            blob__isnull=False,
        )
        .filter(
            Q(parse_result__isnull=True)
            | Q(
                parse_result__status=AIStatus.FAILED,
                parse_result__updated_at__lte=cutoff,
                parse_result__recovery_attempts__lt=max_attempts,
            )
            | Q(
                parse_result__status=AIStatus.PENDING,
                parse_result__updated_at__lte=cutoff,
                parse_result__recovery_attempts__lt=max_attempts,
            )
            | Q(
                parse_result__status=AIStatus.PROCESSING,
                parse_result__updated_at__lte=cutoff,
                parse_result__recovery_attempts__lt=max_attempts,
            )
        )
        .order_by("id")
        .values_list("id", flat=True)
    )
    chunk_gap_ids = DocumentParseResult.objects.filter(
        node__trashed=False,
        node__ai_processing_enabled=True,
        node__blob__isnull=False,
        status=AIStatus.COMPLETED,
        recovery_attempts__lt=max_attempts,
    ).annotate(
        actual_chunk_rows=Count("chunks", distinct=True),
    ).filter(
        actual_chunk_rows__lt=F("chunk_count")
    ).values_list("node_id", flat=True)
    node_ids.update(chunk_gap_ids)
    return sorted(node_ids)[:limit]


def _get_embedding_recovery_chunk_ids(limit: int) -> list[int]:
    cutoff = _recovery_cutoff()
    max_attempts = _get_max_recovery_attempts()
    store = get_embedding_store_instance()
    existing_embedding_qs = store.completed_embedding_exists(chunk_id_ref=OuterRef("pk"))
    chunk_qs = (
        DocumentChunk.objects.select_related("parse_result", "parse_result__node")
        .annotate(
            has_completed_embedding=Exists(existing_embedding_qs),
        )
        .filter(
            parse_result__node__ai_processing_enabled=True,
            parse_result__status=AIStatus.COMPLETED,
            recovery_attempts__lt=max_attempts,
        )
        .filter(
            Q(status=AIStatus.PENDING, updated_at__lte=cutoff)
            | Q(status=AIStatus.PENDING, created_at__lte=cutoff)
            | Q(status=AIStatus.FAILED, updated_at__lte=cutoff)
            | Q(status=AIStatus.FAILED, created_at__lte=cutoff)
            | Q(status=AIStatus.PROCESSING, updated_at__lte=cutoff)
            | Q(status=AIStatus.PROCESSING, created_at__lte=cutoff)
        )
        .filter(has_completed_embedding=False)
        .order_by("id")
    )
    return list(chunk_qs.values_list("id", flat=True)[:limit])


def _reset_chunks_to_pending(chunk_ids: list[int]) -> int:
    if not chunk_ids:
        return 0
    return DocumentChunk.objects.filter(id__in=chunk_ids).exclude(status=AIStatus.PENDING).update(
        status=AIStatus.PENDING,
        error_message={},
    )


def _get_node_ids_for_chunks(chunk_ids: list[int]) -> list[int]:
    """chunk_ids에 해당하는 고유 node_id 목록을 반환합니다."""
    if not chunk_ids:
        return []
    return list(
        DocumentChunk.objects
        .filter(id__in=chunk_ids)
        .order_by()
        .values_list("parse_result__node_id", flat=True)
        .distinct()
    )


def _redis_client():
    """복구 idempotency 체크용 Redis 클라이언트를 반환합니다."""
    from redis import Redis
    redis_url = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    return Redis.from_url(redis_url, socket_connect_timeout=2, socket_timeout=2)


def _embedding_queue_backpressure() -> tuple[bool, int | None, int]:
    """Return whether new parsing should pause behind the embed backlog.

    Redis is the Celery broker, so the queue list length is the authoritative
    count of waiting embed tasks. A broker inspection failure is fail-open:
    Celery itself will still provide delivery/retry semantics, and a transient
    monitoring failure must not stop document ingestion.
    """
    limit = _get_positive_int_env("DOCUMENT_AI_EMBED_QUEUE_BACKPRESSURE_LIMIT", 32)
    try:
        depth = int(_redis_client().llen("embed"))
    except Exception as exc:
        logger.warning(
            "Unable to inspect embed queue depth; parse backpressure is temporarily disabled: %s",
            exc,
        )
        return False, None, limit
    return depth >= limit, depth, limit


def _try_acquire_recovery_lock(redis_client, key: str, ttl_seconds: int) -> bool:
    """Redis SET NX 로 복구 락을 획득합니다.

    락 획득 성공(중복 아님) → True
    이미 존재(중복 큐잉 방지 대상) → False
    """
    return bool(redis_client.set(key, "1", nx=True, ex=ttl_seconds))


@shared_task(queue="parse")
def recover_document_pipeline_backlog() -> dict:
    parse_limit = _get_recovery_parse_batch_size()
    embed_limit = _get_recovery_embedding_batch_size()
    stale_minutes = _get_recovery_stale_minutes()
    lock_ttl = stale_minutes * 60

    # Redis 연결 실패 시 dedup 없이 정상 진행 (graceful fallback)
    try:
        redis = _redis_client()
    except Exception as e:
        logger.warning("Redis unavailable for recovery dedup, proceeding without dedup: %s", e)
        redis = None

    # --- 파싱 복구 ---
    parse_node_ids = _get_parse_recovery_node_ids(parse_limit)
    recovered_parse_count = 0
    skipped_parse_count = 0
    queued_parse_node_ids = []
    for node_id in parse_node_ids:
        if redis is not None:
            lock_key = f"recovery:parse:{node_id}"
            if not _try_acquire_recovery_lock(redis, lock_key, lock_ttl):
                skipped_parse_count += 1
                logger.debug("Parse recovery dedup skip: node_id=%s", node_id)
                continue
        parse_document_with_docling.delay(node_id, **enqueue_kwargs())
        queued_parse_node_ids.append(node_id)
        recovered_parse_count += 1

    # 파싱 복구 메타데이터 업데이트 (재큐잉된 parse result 만)
    if queued_parse_node_ids:
        now = timezone.now()
        DocumentParseResult.objects.filter(node_id__in=queued_parse_node_ids).update(
            recovery_attempts=F("recovery_attempts") + 1,
            last_recovered_at=now,
        )

    # --- 임베딩 복구 ---
    # 복구된 청크를 node 단위로 묶어 enqueue_embedding_tasks 를 경유합니다.
    # enqueue_embedding_tasks 는 PENDING 청크를 PROCESSING 으로 전환한 뒤
    # embedding_document_with_bge 를 큐에 넣으므로, 상태 검사를 올바르게 통과합니다.
    # (기존: embedding_document_with_bge 를 직접 호출 → PROCESSING 상태가 아니어서 skip 됨)
    chunk_ids = _get_embedding_recovery_chunk_ids(embed_limit)
    reset_count = _reset_chunks_to_pending(chunk_ids)

    # chunk_id → node_id 매핑을 미리 구성해 dedup skip 된 노드의 청크는 제외
    chunk_to_node = dict(
        DocumentChunk.objects
        .filter(id__in=chunk_ids)
        .values_list("id", "parse_result__node_id")
    ) if chunk_ids else {}
    node_to_chunks: dict[int, list[int]] = {}
    for cid, nid in chunk_to_node.items():
        node_to_chunks.setdefault(nid, []).append(cid)

    embed_node_ids = list(node_to_chunks.keys())
    recovered_embed_count = 0
    skipped_embed_count = 0
    queued_embed_chunk_ids: list[int] = []
    for node_id in embed_node_ids:
        if redis is not None:
            lock_key = f"recovery:embed:{node_id}"
            if not _try_acquire_recovery_lock(redis, lock_key, lock_ttl):
                skipped_embed_count += 1
                logger.debug("Embed recovery dedup skip: node_id=%s", node_id)
                continue
        enqueue_embedding_tasks.delay(node_id, **enqueue_kwargs())
        queued_embed_chunk_ids.extend(node_to_chunks.get(node_id, []))
        recovered_embed_count += 1

    # 임베딩 복구 메타데이터 업데이트 (실제 재큐잉된 청크만)
    if queued_embed_chunk_ids:
        now = timezone.now()
        DocumentChunk.objects.filter(id__in=queued_embed_chunk_ids).update(
            recovery_attempts=F("recovery_attempts") + 1,
            last_recovered_at=now,
        )

    summary = {
        "status": "success",
        "parse_requeued": recovered_parse_count,
        "parse_skipped_dedup": skipped_parse_count,
        "embedding_nodes_requeued": recovered_embed_count,
        "embedding_nodes_skipped_dedup": skipped_embed_count,
        "chunks_reset_to_pending": reset_count,
        "stale_minutes": stale_minutes,
        "max_recovery_attempts": _get_max_recovery_attempts(),
    }
    logger.info("Recovered document pipeline backlog: %s", summary)
    return summary



def save_parse_result(node, pr: ParseResult) -> DocumentParseResult:
    """
    Pydantic ParseResult → Django ORM 매핑
    DocumentParseResult + DocumentChunk 를 DB에 저장
    """
    metadata = {
        "parser_backend": pr.parser_backend,
        "parser_version": pr.parser_version,
        "tokenizer_name": get_parser_tokenizer_id(),
        "chunk_max_tokens": get_chunk_max_tokens(),
        "embedding_max_tokens": get_embedding_max_tokens(),
        "embedding_backend": get_embedding_backend(),
        "file_ext": pr.file_ext,
    }

    metadata = {k:v for k, v in metadata.items() if v is not None}

    raw_status = (pr.status or "").lower()
    parse_status = AIStatus.FAILED
    if raw_status in {"success", "ok", "done"}:
        parse_status = AIStatus.COMPLETED
    elif raw_status in {"failed", "error"}:
        parse_status = AIStatus.FAILED
    elif pr.chunks and not pr.errors:
        parse_status = AIStatus.COMPLETED

    with transaction.atomic():
        doc_result, _ = DocumentParseResult.objects.update_or_create(
            node=node,
            defaults = {
                "parser_name": pr.parser_backend,
                "parser_mode": pr.parser_mode or "",
                "status": parse_status,
                "input_format": pr.input_format or "",
                "input_document_hash": pr.input_document_hash or "",
                "input_page_count": pr.input_page_count,
                "result_page_count": pr.page_count,
                "chunk_count": len(pr.chunks),
                "timings": pr.timings or {},
                "errors": pr.errors or [],
                "parsed_at": timezone.now(),
                "metadata": metadata,
            }
        )

        # 기존 청크 삭제 후 재생성 (재파싱 대응)
        doc_result.chunks.all().delete()

        chunk_objects = []
        for chunk in pr.chunks:
            # meta에서 검색 보조 메타데이터 추출
            raw_meta = chunk.meta or {}

            serialized_meta = serialize_meta(raw_meta) or {}

            normalized_text = normalize_extracted_text(chunk.serialized_text)

            chunk_objects.append(
                DocumentChunk(
                    parse_result=doc_result,
                    chunk_index=chunk.chunk_index,
                    text=normalized_text,
                    token_count=chunk.tokens,
                    section_title=_extract_section_title(raw_meta),
                    page_from=_extract_page(raw_meta, "page_from"),
                    page_to=_extract_page(raw_meta, "page_to"),
                    chunk_meta=serialized_meta,
                )
            )

        DocumentChunk.objects.bulk_create(chunk_objects)

        # Chunk 무결성 검증
        actual_chunk_count = len(chunk_objects)
        expected_chunk_count = len(pr.chunks)
        if actual_chunk_count != expected_chunk_count:
            raise ValueError(
                f"Chunk count mismatch: expected {expected_chunk_count}, got {actual_chunk_count}"
            )

        logger.info(
            "Chunks saved: node_id=%s, parse_result_id=%s, status=%s, chunks=%s, parser_mode=%s",
            node.id,
            doc_result.id,
            parse_status,
            actual_chunk_count,
            pr.parser_mode or "",
        )

        return doc_result


def _extract_section_title(meta: dict) -> str:
    """meta에서 섹션 제목을 추출하는 헬퍼"""
    # docling meta 구조에 따라 headings 또는 doc_items에서 추출
    headings = meta.get("headings", [])
    if headings:
        return " > ".join(headings)
    return ""


def _extract_page(meta: dict, key: str) -> int | None:
    """meta에서 페이지 번호를 추출하는 헬퍼"""
    # docling meta 구조에 따라 page 정보 추출
    doc_items = meta.get("doc_items", [])
    if doc_items:
        prov = doc_items[0].get("prov", [])
        if prov:
            page = prov[0].get("page_no")
            if page is not None:
                return page
    return None


@shared_task(
    queue="parse",
    bind=True,
    max_retries=2,
    retry_backoff=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def parse_document_with_docling(
    self, node_id: int, trace_id: str | None = None, enqueued_at: str | None = None
) -> dict:
    """
    Celery 태스크: 파싱 → DB 저장 오케스트레이션
    node_id를 받아 파일 경로를 조회하고, 파싱 후 결과를 DB에 저장
    """
    from files.models import Node
    from document_ai.parsers.registry import get_document_parser

    parser = get_document_parser()

    set_trace_id(trace_id or new_trace_id())
    task_start = timezone.now()
    worker_started = perf_counter()
    enqueued_dt = datetime.fromisoformat(enqueued_at) if enqueued_at else None

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

    try:
        with capture_db_spans():
            try:
                node = Node.objects.select_related("blob").get(pk=node_id)
                if node.node_type != "file":
                    raise ValueError(f"Node {node_id} is not a file")
                if not node.ai_processing_enabled:
                    logger.info("Parse skipped: node_id=%s, reason=ai_processing_disabled", node_id)
                    return {
                        "status": "skipped",
                        "node_id": node_id,
                        "message": "AI processing is disabled",
                    }
                if not hasattr(node, "blob") or not node.blob.file:
                    raise ValueError(f"Node {node_id} has no attached file blob")

                file_path = node.blob.file.path

                logger.info("Parse started: node_id=%s", node_id)

                # 1. 파서 호출 (순수 Pydantic 결과)
                parse_result = parser.parse(file_path)

                node.refresh_from_db(fields=["ai_processing_enabled"])
                if not node.ai_processing_enabled:
                    logger.info("Parse result discarded: node_id=%s, reason=ai_processing_disabled_after_parse", node_id)
                    return {
                        "status": "skipped",
                        "node_id": node_id,
                        "message": "AI processing was disabled during parsing",
                    }

                # 2. DB 저장 (Pydantic → ORM 매핑)
                doc_result = save_parse_result(node, parse_result)

                metrics = dict(doc_result.performance_metrics or {})
                put_metric(metrics, "trace_id", get_trace_id())
                put_metric(metrics, "queue_wait_ms", datetime_delta_ms(enqueued_dt, task_start))
                put_metric(metrics, "parse_processing_ms", elapsed_ms(worker_started))
                doc_result.performance_metrics = metrics
                doc_result.save(update_fields=["performance_metrics"])
            except Node.DoesNotExist:
                logger.error(f"Node {node_id} not found")
                return {"status": "failed", "error": f"Node {node_id} not found"}

        enqueue_embedding_tasks.delay(node_id, **enqueue_kwargs(trace_id=get_trace_id()))

        logger.info(
            "Parse completed: node_id=%s, status=%s, chunks=%s, parser_mode=%s",
            node_id,
            doc_result.status,
            doc_result.chunk_count,
            parse_result.parser_mode or "",
        )

        return {
            "status": "success",
            "node_id": node_id,
            "chunk_count": doc_result.chunk_count,
        }

    except Exception as e:
        logger.exception(f"파싱 실패: node_id={node_id}")

        failure_metrics = {}
        put_metric(failure_metrics, "trace_id", get_trace_id())
        put_metric(failure_metrics, "queue_wait_ms", datetime_delta_ms(enqueued_dt, task_start))
        put_metric(failure_metrics, "parse_processing_ms", elapsed_ms(worker_started))
        failure_metrics["failed"] = True

        # 실패 상태 기록
        DocumentParseResult.objects.update_or_create(
            node_id=node_id,
            defaults={
                "parser_name": parser.spec.backend,
                "status": AIStatus.FAILED,
                "errors": [{"message": str(e)}],
                "performance_metrics": failure_metrics,
                "metadata": {
                    "tokenizer_name": get_embedding_model(),
                    "chunk_max_tokens": get_chunk_max_tokens(),
                    "embedding_max_tokens": get_embedding_max_tokens(),
                    "embedding_backend": get_embedding_backend(),
                },
                "parsed_at": timezone.now(),
            },
        )

        if isinstance(e, (OSError, TimeoutError, ConnectionError)) and self.request.retries < self.max_retries:
            raise self.retry(exc=e)

        return {
            "status": "failed",
            "node_id": node_id,
            "error": str(e),
        }

@shared_task(queue="embed")
def run_quality_evaluation_task(run_uid: str) -> None:
    """Runs a workspace quality-profile evaluation (WorkspaceQualityEvaluationRun)
    and persists its metrics. Defined here rather than in document_ai.search.evaluation
    because Celery's autodiscover_tasks() only imports each app's tasks.py module."""
    from document_ai.search.evaluation import execute_quality_evaluation_run

    execute_quality_evaluation_run(run_uid)


@shared_task(queue="embed")
def enqueue_embedding_tasks(
    node_id: int,
    trace_id: str | None = None,
    enqueued_at: str | None = None,
) -> dict:
    """
    Celery 태스크: 임베딩 → DB 저장 오케스트레이션
    node_id를 받아 파일 경로를 조회하고, 임베딩 후 결과를 DB에 저장
    """
    from document_ai.processing.embedding import enqueue_embedding_tasks_sync

    # `enqueued_at` is part of the common Celery tracing envelope. This
    # orchestration task does not persist its own queue-wait metric, but it
    # must accept the envelope before it fans out chunk tasks that do.
    _ = enqueued_at
    return enqueue_embedding_tasks_sync(node_id, trace_id=trace_id)


@shared_task(
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
    try:
        from document_ai.processing.embedding import RetryableEmbeddingError, embed_document_chunk_sync

        return embed_document_chunk_sync(
            chunk_id,
            trace_id=trace_id,
            enqueued_at=enqueued_at,
            retries=self.request.retries,
            max_retries=self.max_retries,
        )
    except RetryableEmbeddingError as exc:
        raise self.retry(exc=exc)


@shared_task(
    queue="embed",
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def embedding_document_batch_with_bge(
    self, chunk_ids: list[int], trace_id: str | None = None, enqueued_at: str | None = None
) -> dict:
    """
    Celery 태스크: 같은 문서의 청크 묶음을 model.encode() 한 번으로 임베딩.
    enqueue_embedding_tasks_sync가 청크 수/토큰 예산으로 미리 묶어 넘긴 chunk_ids를
    받아 처리한다. 실패 시 배치 전체를 하나의 단위로 재시도한다.
    """
    try:
        from document_ai.processing.embedding import RetryableEmbeddingError, embed_document_chunks_batch_sync

        return embed_document_chunks_batch_sync(
            chunk_ids,
            trace_id=trace_id,
            enqueued_at=enqueued_at,
            retries=self.request.retries,
            max_retries=self.max_retries,
        )
    except RetryableEmbeddingError as exc:
        raise self.retry(exc=exc)


def _summarize_structured_filters(filters: list[dict]) -> list[str]:
    summaries = []
    for item in filters:
        scope = item.get("scope") or "node"
        field = item.get("field")
        operator = item.get("operator")
        value = item.get("value")
        if not field or not operator:
            continue
        summaries.append(f"{scope}.{field} {operator} {value}")
    return summaries


def _build_refined_query_prompt(*, mode: str, question: dict, metadata: dict) -> str:
    dense_query = (question.get("dense") or question.get("residual") or question.get("normalized") or "").strip()
    if not dense_query:
        return ""

    filter_summaries = _summarize_structured_filters(metadata.get("filters") or [])
    scope_text = ", ".join(metadata.get("target_scopes") or [])
    prompt_lines = [dense_query]

    if filter_summaries:
        prompt_lines.append("메타데이터 조건: " + "; ".join(filter_summaries))
    if scope_text:
        prompt_lines.append("대상 범위: " + scope_text)

    if mode == "rag":
        prompt_lines.append(
            "검색된 문서 근거만 사용해 한국어로 답변하고, 확인되지 않은 내용은 근거 부족으로 분리하세요."
        )
    else:
        prompt_lines.append("메타데이터 조건을 먼저 적용한 뒤 남은 의미 질의로 관련 문서를 검색하세요.")

    return "\n".join(prompt_lines)


@shared_task(queue="query")
def parse_user_query(query: str, mode: str = "search") -> dict:
    """
    사용자 질의를 LLM 기반 QueryDSL 후보로 파싱하고 query_engine에서 검증/ORM 컴파일합니다.
    규칙 기반 QueryAnalyzer는 사용하지 않으며, 실패 시 원 질의를 semantic query로 보존합니다.
    """
    from document_ai.query_understanding.parser_service import parse_user_query_sync

    return parse_user_query_sync(query, mode)
