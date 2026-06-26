from __future__ import annotations

import logging

from django.db import transaction

from config.enums import AIStatus
from document_ai.models import DocumentChunk
from document_ai.parsers.config import (
    get_embedding_backend,
    get_embedding_max_tokens,
    get_embedding_model,
    get_hf_tokenizer,
    get_raw_tokenizer,
)
from document_ai.parsers.text_utils import normalize_extracted_text

logger = logging.getLogger(__name__)


class RetryableEmbeddingError(Exception):
    pass


def truncate_embedding_input_text(text: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        raise ValueError("Embedding max_tokens must be greater than zero.")

    raw_tokenizer = get_raw_tokenizer()
    token_ids = raw_tokenizer.encode(text, add_special_tokens=False)
    if len(token_ids) <= max_tokens:
        return text

    truncated_ids = token_ids[:max_tokens]
    truncated_text = raw_tokenizer.decode(
        truncated_ids,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=True,
    ).strip()
    return truncated_text or text


def prepare_embedding_input_text(text: str, *, chunk=None) -> tuple[str, int, bool]:
    max_tokens = get_embedding_max_tokens()
    tokenizer = get_hf_tokenizer()
    token_count = tokenizer.count_tokens(text)
    if token_count > max_tokens:
        chunk_label = f"chunk_id={chunk.id}, " if chunk is not None else ""
        truncated_text = truncate_embedding_input_text(text, max_tokens)
        truncated_token_count = tokenizer.count_tokens(truncated_text)
        logger.warning(
            "Embedding input truncated: %stokens=%s, truncated_tokens=%s, max_tokens=%s",
            chunk_label,
            token_count,
            truncated_token_count,
            max_tokens,
        )
        return truncated_text, truncated_token_count, True
    return text, token_count, False


def enqueue_embedding_tasks_sync(node_id: int) -> dict:
    try:
        with transaction.atomic():
            from files.models import Node

            if not Node.objects.filter(pk=node_id, ai_processing_enabled=True).exists():
                logger.info("Embedding queue skipped: node_id=%s, reason=ai_processing_disabled_or_missing", node_id)
                return {
                    "status": "skipped",
                    "node_id": node_id,
                    "chunk_count": 0,
                    "message": "AI processing is disabled or node is missing",
                }

            chunk_ids = list(
                DocumentChunk.objects
                .select_for_update(skip_locked=True)
                .filter(
                    parse_result__node_id=node_id,
                    parse_result__node__ai_processing_enabled=True,
                    status=AIStatus.PENDING,
                )
                .values_list("id", flat=True)
            )

            if not chunk_ids:
                logger.info("Embedding queue skipped: node_id=%s, reason=no_pending_chunks", node_id)
                return {
                    "status": "success",
                    "node_id": node_id,
                    "chunk_count": 0,
                    "message": "No pending chunks found",
                }

            DocumentChunk.objects.filter(id__in=chunk_ids).update(
                status=AIStatus.PROCESSING
            )

        from document_ai.tasks import embedding_document_with_bge

        for chunk_id in chunk_ids:
            embedding_document_with_bge.apply_async(args=[chunk_id], queue="embed")

        logger.info(
            "Embedding queued: node_id=%s, chunks=%s",
            node_id,
            len(chunk_ids),
        )

        return {
            "status": "success",
            "node_id": node_id,
            "chunk_count": len(chunk_ids),
        }

    except Exception as exc:
        logger.exception("bge-embedding queueing failed: node_id=%s", node_id)
        return {
            "status": "failed",
            "node_id": node_id,
            "error": str(exc),
        }


def _mark_embedding_failed(chunk, *, embedding_model: str, embedding_backend: str, error_message: str) -> None:
    from document_ai.embedding.store_registry import get_embedding_store_instance

    store = get_embedding_store_instance(
        model_name=embedding_model,
        backend=embedding_backend,
    )
    store.mark_chunk_embedding_failed(chunk=chunk, error_message=error_message)

    chunk.status = AIStatus.FAILED
    chunk.error_message = error_message
    chunk.save(update_fields=["status", "error_message"])


def embed_document_chunk_sync(chunk_id: int, *, retries: int = 0, max_retries: int = 3) -> dict:
    embedding_model = get_embedding_model()
    embedding_backend = get_embedding_backend()

    try:
        chunk = DocumentChunk.objects.select_related("parse_result__node").get(pk=chunk_id)
    except DocumentChunk.DoesNotExist:
        logger.error("Chunk %s not found", chunk_id)
        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": f"Chunk {chunk_id} not found",
        }

    if not chunk.parse_result.node.ai_processing_enabled:
        logger.info(
            "Embedding skipped: chunk_id=%s, node_id=%s, reason=ai_processing_disabled",
            chunk_id,
            chunk.parse_result.node_id,
        )
        if chunk.status == AIStatus.PROCESSING:
            chunk.status = AIStatus.PENDING
            chunk.error_message = {}
            chunk.save(update_fields=["status", "error_message"])
        return {
            "status": "skipped",
            "chunk_id": chunk_id,
            "node_id": chunk.parse_result.node_id,
            "message": "AI processing is disabled",
        }

    if chunk.status != AIStatus.PROCESSING:
        logger.warning("Chunk %s skipped: %s", chunk_id, chunk.status)
        return {
            "status": "skipped",
            "chunk_id": chunk_id,
            "message": f"Chunk {chunk_id} is not valid state",
        }

    try:
        text = normalize_extracted_text(chunk.text or "")
        if not text:
            raise ValueError("Chunk text is empty")
        text, input_token_count, input_truncated = prepare_embedding_input_text(text, chunk=chunk)

        logger.info(
            "Embedding started: chunk_id=%s, node_id=%s, chunk_index=%s, stored_tokens=%s, input_tokens=%s, max_tokens=%s, truncated=%s, model=%s, backend=%s",
            chunk_id,
            chunk.parse_result.node_id,
            chunk.chunk_index,
            chunk.token_count,
            input_token_count,
            get_embedding_max_tokens(),
            input_truncated,
            embedding_model,
            embedding_backend,
        )

        from document_ai.embedding.embeding_models import embed_document

        embedding = embed_document(
            text=text,
            model_name=embedding_model,
            backend=embedding_backend,
        )

        from document_ai.embedding.store_registry import get_embedding_store_instance

        store = get_embedding_store_instance(
            model_name=embedding_model,
            backend=embedding_backend,
        )
        store.save_chunk_embedding(chunk=chunk, embedding=embedding, status=AIStatus.COMPLETED)

        chunk.status = AIStatus.COMPLETED
        chunk.error_message = ""
        chunk.save(update_fields=["status", "error_message"])

        logger.info(
            "Embedding completed: chunk_id=%s, node_id=%s, dense_dim=%s, sparse_terms=%s, model=%s, backend=%s",
            chunk_id,
            chunk.parse_result.node_id,
            len(embedding.dense_vector),
            len(embedding.sparse_vector),
            embedding_model,
            embedding_backend,
        )

        return {
            "status": "success",
            "chunk_id": chunk_id,
        }

    except ValueError as exc:
        logger.warning("Embedding validation failed: chunk_id=%s, error=%s", chunk_id, exc)
        _mark_embedding_failed(
            chunk,
            embedding_model=embedding_model,
            embedding_backend=embedding_backend,
            error_message=str(exc),
        )
        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": str(exc),
        }

    except RuntimeError as exc:
        error_message = str(exc)
        if "GPU OOM" in error_message:
            logger.warning(
                "Embedding GPU OOM: chunk_id=%s, retries=%s/%s, error=%s",
                chunk_id,
                retries,
                max_retries,
                error_message,
            )
        else:
            logger.warning(
                "Embedding runtime error: chunk_id=%s, retries=%s/%s, error=%s",
                chunk_id,
                retries,
                max_retries,
                error_message,
            )

        if retries < max_retries:
            raise RetryableEmbeddingError(error_message) from exc

        logger.exception("Embedding permanently failed: chunk_id=%s", chunk_id)
        _mark_embedding_failed(
            chunk,
            embedding_model=embedding_model,
            embedding_backend=embedding_backend,
            error_message=error_message,
        )
        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": error_message,
        }

    except Exception as exc:
        logger.warning(
            "Embedding failed (retrying): chunk_id=%s, retries=%s/%s, error=%s",
            chunk_id,
            retries,
            max_retries,
            exc,
        )

        if retries < max_retries:
            raise RetryableEmbeddingError(str(exc)) from exc

        logger.exception("Embedding permanently failed: chunk_id=%s", chunk_id)
        _mark_embedding_failed(
            chunk,
            embedding_model=embedding_model,
            embedding_backend=embedding_backend,
            error_message=str(exc),
        )
        return {
            "status": "failed",
            "chunk_id": chunk_id,
            "error": str(exc),
        }
