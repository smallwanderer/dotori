from __future__ import annotations

import json
import logging
import os
import re
import time as time_module

from django.utils import timezone

from config.enums import AIStatus, QueryIntent, RAGStage

logger = logging.getLogger(__name__)


class RAGGenerationBusy(Exception):
    pass


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
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON RAG stream line: %r", line[:200])
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


def _build_rag_context(
    results: list,
    *,
    evidence_limit: int = 3,
    context_max_chars: int = 3000,
    evidence_text_max_chars: int = 500,
) -> tuple[str, list[dict]]:
    context_blocks = []
    citations = []
    citation_id = 1
    used_chars = 0

    for result in results or []:
        node_name = result.get("node_name", "")
        node_id = result.get("node_id")
        doc_score = result.get("doc_score")
        for evidence in result.get("evidences", []) or []:
            if citation_id > evidence_limit:
                return "\n\n".join(context_blocks), citations

            text = evidence.get("compressed_text") or evidence.get("context_text") or evidence.get("text") or ""
            text = str(text).strip()
            if not text:
                continue
            remaining_chars = context_max_chars - used_chars
            if remaining_chars <= 0:
                return "\n\n".join(context_blocks), citations

            text_limit = max(100, min(evidence_text_max_chars, remaining_chars))
            clipped_text = text[:text_limit]
            citation = {
                "id": citation_id,
                "node_id": str(node_id) if node_id is not None else "",
                "node_name": node_name,
                "chunk_id": evidence.get("chunk_id"),
                "section": evidence.get("section") or "",
                "pages": evidence.get("pages") or "",
                "doc_score": doc_score,
                "hybrid_score": evidence.get("hybrid_score"),
                "dense_score": evidence.get("dense_score"),
                "sparse_score": evidence.get("sparse_score"),
                "text": clipped_text,
            }
            block = "\n".join(
                [
                    f"[{citation_id}] {node_name}",
                    f"section: {citation['section'] or 'N/A'}",
                    f"pages: {citation['pages'] or 'N/A'}",
                    f"text: {citation['text']}",
                ]
            )
            if used_chars + len(block) > context_max_chars and citations:
                return "\n\n".join(context_blocks), citations

            citations.append(citation)
            context_blocks.append(block)
            used_chars += len(block) + 2
            citation_id += 1

    return "\n\n".join(context_blocks), citations


def _coerce_score(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _citation_best_score(citation: dict) -> float | None:
    doc_score = _coerce_score(citation.get("doc_score"))
    if doc_score is not None:
        return doc_score

    score_candidates = [
        citation.get("hybrid_score"),
        citation.get("dense_score"),
        citation.get("sparse_score"),
    ]
    scores = [_coerce_score(value) for value in score_candidates]
    scores = [score for score in scores if score is not None]
    return max(scores) if scores else None


def _evidence_is_sufficient(citations: list[dict], threshold: float | None) -> bool:
    if not citations:
        return False
    if threshold is None:
        return True

    saw_numeric_score = False
    for citation in citations:
        score = _citation_best_score(citation)
        if score is None:
            continue
        saw_numeric_score = True
        if score >= threshold:
            return True

    return not saw_numeric_score


def _insufficient_evidence_answer(language: str, *, scoped: bool) -> str:
    if language == "ko":
        if scoped:
            return (
                "선택한 문서 범위에서 이 질문에 답할 충분한 근거를 찾지 못했습니다.\n"
                "질문 범위를 넓히거나 다른 문서를 선택해 다시 시도해 주세요."
            )
        return "현재 문서에서 이 질문과 직접 관련된 충분한 근거를 찾지 못했습니다."

    if scoped:
        return (
            "I could not find enough evidence in the selected document scope to answer this question.\n"
            "Try broadening the scope or selecting different documents."
        )
    return "I could not find enough directly relevant evidence in the current documents to answer this question."


def _uses_openai_responses_api(base_url: str) -> bool:
    normalized = (base_url or "").strip().lower().rstrip("/")
    return normalized == "https://api.openai.com" or normalized.endswith(".openai.com")


def _build_responses_payload(
    *,
    llm_config,
    system_prompt: str,
    fewshot_user: str,
    fewshot_assistant: str,
    user_prompt: str,
    max_tokens: int,
) -> dict:
    payload = {
        "model": llm_config.model,
        "instructions": system_prompt,
        "input": [
            {"role": "user", "content": fewshot_user},
            {"role": "assistant", "content": fewshot_assistant},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": float(os.getenv("RAG_TEMPERATURE", "0.2")),
        "top_p": float(os.getenv("RAG_TOP_P", "0.9")),
        "stream": True,
    }
    token_limit_key = "max_output_tokens" if _uses_openai_responses_api(llm_config.base_url) else "max_tokens"
    payload[token_limit_key] = max_tokens
    return payload


def _build_chat_completions_payload(
    *,
    llm_config,
    system_prompt: str,
    fewshot_user: str,
    fewshot_assistant: str,
    user_prompt: str,
    max_tokens: int,
) -> dict:
    return {
        "model": llm_config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": fewshot_user},
            {"role": "assistant", "content": fewshot_assistant},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": float(os.getenv("RAG_TEMPERATURE", "0.2")),
        "top_p": float(os.getenv("RAG_TOP_P", "0.9")),
        "max_tokens": max_tokens,
        "stream": True,
    }


def _build_llm_request(*, llm_config, system_prompt, fewshot_user, fewshot_assistant, user_prompt, max_tokens):
    if _uses_openai_responses_api(llm_config.base_url):
        return (
            llm_config.responses_url,
            _build_responses_payload(
                llm_config=llm_config,
                system_prompt=system_prompt,
                fewshot_user=fewshot_user,
                fewshot_assistant=fewshot_assistant,
                user_prompt=user_prompt,
                max_tokens=max_tokens,
            ),
        )

    return (
        llm_config.chat_completions_url,
        _build_chat_completions_payload(
            llm_config=llm_config,
            system_prompt=system_prompt,
            fewshot_user=fewshot_user,
            fewshot_assistant=fewshot_assistant,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
        ),
    )


def _normalize_rag_answer(raw_answer: str) -> str:
    answer = _extract_llm_final_content(raw_answer, fallback_to_raw=True)
    if not answer:
        return ""

    answer = re.sub(r"<\|channel\>thought[\s\S]*?(?=<\|channel\>final|$)", "", answer).strip()
    answer = re.sub(r"<\|[^>]+?\|>", "", answer).strip()
    answer = re.sub(r"^```(?:[a-zA-Z]+)?\s*|\s*```$", "", answer).strip()
    return answer


def _clean_rag_evidence_text(text: str, *, limit: int = 220) -> str:
    cleaned = re.sub(r"<!--.*?-->", " ", str(text or ""), flags=re.DOTALL)
    cleaned = re.sub(r"\|[-:\s|]+\|", " ", cleaned)
    cleaned = re.sub(r"[|#*_`]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + "..."
    return cleaned


def _fallback_rag_answer(citations: list[dict], language: str) -> str:
    if not citations:
        return "근거가 부족해 답변할 수 없습니다." if language == "ko" else "There is not enough evidence to answer."

    selected = citations[:3]

    if language == "ko":
        lines = ["핵심 답변"]
        for citation in selected[:2]:
            text = _clean_rag_evidence_text(citation.get("text"), limit=230)
            if text:
                lines.append(f"- {text} [{citation.get('id')}]")

        lines.append("")
        lines.append("주요 근거")
        for citation in selected:
            section = citation.get("section") or "N/A"
            pages = citation.get("pages") or "N/A"
            node_name = citation.get("node_name") or "문서"
            lines.append(f"- {node_name} / section {section} / page {pages} [{citation.get('id')}]")

        lines.append("")
        lines.append("근거 부족")
        lines.append("- 위 근거 밖의 세부 사항은 확인하지 않았습니다.")
        return "\n".join(lines)

    lines = ["Answer"]
    for citation in selected[:2]:
        text = _clean_rag_evidence_text(citation.get("text"), limit=230)
        if text:
            lines.append(f"- {text} [{citation.get('id')}]")

    lines.append("")
    lines.append("Key Evidence")
    for citation in selected:
        section = citation.get("section") or "N/A"
        pages = citation.get("pages") or "N/A"
        node_name = citation.get("node_name") or "Document"
        lines.append(f"- {node_name} / section {section} / page {pages} [{citation.get('id')}]")

    lines.append("")
    lines.append("Limitations")
    lines.append("- Details outside the cited evidence were not verified.")
    return "\n".join(lines)


def _mark_rag_canceled(rag_job, redis_client) -> dict:
    from document_ai.services.rag_cancel_service import clear_rag_cancel_signal

    rag_job.status = AIStatus.CANCELED
    rag_job.stage = RAGStage.CANCELED
    rag_job.stage_message = "사용자 요청으로 RAG 작업을 중단했습니다."
    rag_job.canceled_at = rag_job.canceled_at or timezone.now()
    rag_job.completed_at = rag_job.completed_at or timezone.now()
    rag_job.save(update_fields=["status", "stage", "stage_message", "canceled_at", "completed_at"])
    clear_rag_cancel_signal(rag_job.id, redis_client=redis_client)
    return {"status": "canceled", "job_id": rag_job.id}


def _build_generation_prompts(rag_job, *, skip_retrieval: bool, context_text: str) -> tuple[str, str, str, str]:
    language_instruction = "Answer in Korean" if rag_job.language == "ko" else "Answer in English."
    if skip_retrieval:
        system_prompt = (
            "You are a helpful AI assistant for this document workspace. "
            "The query classifier determined that document retrieval is not required. "
            "Answer naturally without citations. If the user asks about app usage, explain briefly and practically. "
            "If the user asks casual conversation, respond politely and concisely. "
            "Do not claim that you searched documents. "
            "Absolutely do not output reasoning processes, thoughts, or XML-like thinking tags. "
            "Output only the final clean answer text.\n"
            f"{language_instruction}"
        )
        user_prompt = f"Question:\n{rag_job.question}\n\nAnswer without document citations."
    else:
        system_prompt = (
            "You are a helpful AI assistant. "
            "For general greetings, casual conversation, or helper requests (e.g., 'Hi', 'Hello', '안녕', '반가워', '너는 누구야'), "
            "respond friendly, politely, and naturally. You do not need to look at or cite the evidence for these casual interactions.\n"
            "For informational questions requiring document knowledge, use the provided evidence to answer. "
            "Only answer what is supported by the evidence and append citation numbers like [1], [2] at the end of each cited sentence.\n"
            "If the informational question cannot be answered using the provided evidence, state clearly and politely that "
            "the answer cannot be found in the provided documents, and do not make up an answer.\n"
            "Absolutely do not output reasoning processes, thoughts, or XML-like thinking tags. "
            "Output only the final clean answer text.\n"
            f"{language_instruction}"
        )
        user_prompt = (
            f"Question:\n{rag_job.question}\n\n"
            f"Evidence:\n{context_text}\n\n"
            "Answer the question. If it is an informational question, answer strictly based on the evidence provided."
        )

    if rag_job.language == "ko":
        fewshot_user = (
            "Question:\n주요 지원 대책은 무엇인가요?\n\n"
            "Evidence:\n[1] 문서 A\nsection: 정책\npages: 1\n"
            "text: 정부는 공급 확대와 할인 지원을 병행해 가격 부담을 낮춘다.\n\n"
            "Answer concisely but sufficiently based on the evidence provided."
        )
        fewshot_assistant = "주요 지원 대책은 공급 확대와 할인 지원을 병행하는 것입니다 [1]."
    else:
        fewshot_user = (
            "Question:\nWhat are the main support measures?\n\n"
            "Evidence:\n[1] Document A\nsection: Policy\npages: 1\n"
            "text: The government will expand supply and provide discount support to reduce price pressure.\n\n"
            "Answer using only the evidence above."
        )
        fewshot_assistant = "The main support measures include supply expansion and discount support [1]."

    if skip_retrieval:
        fewshot_user = "Question:\n안녕 너는 뭐 할 수 있어?\n\nAnswer without document citations."
        fewshot_assistant = (
            "안녕하세요. 문서 검색, 요약, 근거 기반 질문 답변을 도와드릴 수 있습니다."
            if rag_job.language == "ko"
            else "Hello. I can help with document search, summaries, and evidence-based answers."
        )

    return system_prompt, user_prompt, fewshot_user, fewshot_assistant


def generate_rag_response_sync(rag_job_id: int) -> dict:
    import requests
    from redis import Redis
    from redis_semaphore import NotAvailable, Semaphore

    from document_ai.models import RAGJob
    from document_ai.services.llm_endpoint_service import resolve_rag_llm_request_config
    from document_ai.services.rag_cancel_service import (
        is_rag_cancel_requested,
    )

    try:
        rag_job = RAGJob.objects.select_related("search_job", "llm_endpoint").get(pk=rag_job_id)
    except RAGJob.DoesNotExist:
        logger.error("RAG job %s not found", rag_job_id)
        return {"status": "failed", "job_id": rag_job_id, "error": f"RAG job {rag_job_id} not found"}

    skip_retrieval = not rag_job.search_job and (
        not rag_job.retrieval_required
        and rag_job.query_intent != QueryIntent.INFORMATION
    )

    if not skip_retrieval and (not rag_job.search_job or rag_job.search_job.status != AIStatus.COMPLETED):
        rag_job.status = AIStatus.FAILED
        rag_job.stage = RAGStage.FAILED
        rag_job.stage_message = "검색 결과가 완료되지 않아 답변을 생성할 수 없습니다."
        rag_job.completed_at = timezone.now()
        rag_job.error_message = "Search job is not completed."
        rag_job.save(update_fields=["status", "stage", "stage_message", "completed_at", "error_message"])
        return {"status": "failed", "job_id": rag_job_id, "error": rag_job.error_message}

    rag_job.status = AIStatus.PROCESSING
    rag_job.stage = RAGStage.GENERATING
    rag_job.stage_message = "검색된 근거를 바탕으로 답변을 생성하고 있습니다."
    rag_job.started_at = timezone.now()
    rag_job.error_message = ""
    rag_job.save(update_fields=["status", "stage", "stage_message", "started_at", "error_message"])

    llm_config = resolve_rag_llm_request_config(rag_job)
    redis_url = os.getenv("RAG_REDIS_URL", os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"))
    semaphore_count = _get_positive_int_env("RAG_SEMAPHORE_COUNT", 1)
    semaphore_timeout = _get_positive_int_env("RAG_SEMAPHORE_TIMEOUT", 60)
    request_timeout = _get_positive_int_env("RAG_REQUEST_TIMEOUT", 300)
    max_tokens = _get_positive_int_env("RAG_MAX_TOKENS", 512)
    evidence_limit = _get_positive_int_env("RAG_EVIDENCE_LIMIT", 5)
    context_max_chars = _get_positive_int_env("RAG_CONTEXT_MAX_CHARS", 3000)
    evidence_text_max_chars = _get_positive_int_env("RAG_EVIDENCE_TEXT_MAX_CHARS", 500)
    stale_lock_timeout = max(request_timeout + 60, 300)
    semaphore_namespace = os.getenv(
        "RAG_SEMAPHORE_NAMESPACE",
        "llama_rag_v1",
    )

    if skip_retrieval:
        context_text, citations = "", []
    else:
        context_text, citations = _build_rag_context(
            rag_job.search_job.results,
            evidence_limit=evidence_limit,
            context_max_chars=context_max_chars,
            evidence_text_max_chars=evidence_text_max_chars,
        )
    if not skip_retrieval and not _evidence_is_sufficient(citations, rag_job.search_job.threshold):
        rag_job.answer = _insufficient_evidence_answer(rag_job.language, scoped=bool(rag_job.node_ids))
        rag_job.status = AIStatus.COMPLETED
        rag_job.stage = RAGStage.COMPLETED
        rag_job.stage_message = "문서에서 충분한 근거를 찾지 못했습니다."
        rag_job.completed_at = timezone.now()
        rag_job.error_message = ""
        rag_job.citations = []
        rag_job.save(update_fields=["answer", "status", "stage", "stage_message", "completed_at", "error_message", "citations"])
        return {"status": "insufficient_evidence", "job_id": rag_job_id, "citation_count": 0}

    system_prompt, user_prompt, fewshot_user, fewshot_assistant = _build_generation_prompts(
        rag_job,
        skip_retrieval=skip_retrieval,
        context_text=context_text,
    )

    redis_client = Redis.from_url(redis_url)
    semaphore = Semaphore(
        redis_client,
        count=semaphore_count,
        namespace=semaphore_namespace,
        stale_client_timeout=stale_lock_timeout,
    )

    acquired = False
    try:
        semaphore.acquire(timeout=semaphore_timeout)
        acquired = True
    except NotAvailable as exc:
        rag_job.status = AIStatus.PENDING
        rag_job.stage = RAGStage.QUEUED
        rag_job.stage_message = "RAG worker가 바빠서 잠시 후 다시 시도합니다."
        rag_job.task_id = ""
        rag_job.error_message = "RAG worker is busy. Please retry shortly."
        rag_job.save(update_fields=["status", "stage", "stage_message", "task_id", "error_message"])
        raise RAGGenerationBusy("RAG worker is busy. Please retry shortly.") from exc

    try:
        if rag_job.status == AIStatus.CANCELED or is_rag_cancel_requested(rag_job.id, redis_client=redis_client):
            return _mark_rag_canceled(rag_job, redis_client)

        request_url, payload = _build_llm_request(
            llm_config=llm_config,
            system_prompt=system_prompt,
            fewshot_user=fewshot_user,
            fewshot_assistant=fewshot_assistant,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
        )
        response = requests.post(
            request_url,
            json=payload,
            headers=llm_config.headers,
            timeout=(5, request_timeout),
            stream=True,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            response_text = response.text[:2000]
            raise RuntimeError(
                f"RAG LLM HTTP {response.status_code}: {response_text}"
            ) from exc
        raw_parts = []
        last_cancel_check = 0.0
        for content in _iter_openai_responses_stream_content(response):
            raw_parts.append(content)
            now_monotonic = time_module.monotonic()
            if now_monotonic - last_cancel_check < 0.5:
                continue
            last_cancel_check = now_monotonic
            if is_rag_cancel_requested(rag_job.id, redis_client=redis_client):
                response.close()
                return _mark_rag_canceled(rag_job, redis_client)

        raw_answer = "".join(raw_parts).strip()
        if is_rag_cancel_requested(rag_job.id, redis_client=redis_client):
            return _mark_rag_canceled(rag_job, redis_client)

        rag_job.refresh_from_db(fields=["status", "stage"])
        if rag_job.status == AIStatus.CANCELED:
            from document_ai.services.rag_cancel_service import clear_rag_cancel_signal

            clear_rag_cancel_signal(rag_job.id, redis_client=redis_client)
            return {"status": "canceled", "job_id": rag_job_id}
        answer = _normalize_rag_answer(raw_answer)
        if not answer:
            logger.warning(
                "RAG answer normalization returned empty output: job_id=%s raw_preview=%r",
                rag_job.id,
                raw_answer[:500],
            )
            answer = _fallback_rag_answer(citations, rag_job.language)
            rag_job.error_message = f"LLM returned no final answer after normalization. raw_preview={raw_answer[:1000]}"
        else:
            rag_job.error_message = ""

        rag_job.answer = answer
        rag_job.citations = citations
        rag_job.status = AIStatus.COMPLETED
        rag_job.stage = RAGStage.COMPLETED
        rag_job.stage_message = "답변 생성이 완료되었습니다."
        rag_job.completed_at = timezone.now()
        rag_job.save(update_fields=["answer", "citations", "status", "stage", "stage_message", "completed_at", "error_message"])

        return {"status": "success", "job_id": rag_job_id, "citation_count": len(citations)}
    except requests.Timeout as exc:
        rag_job.status = AIStatus.FAILED
        rag_job.stage = RAGStage.FAILED
        rag_job.stage_message = "모델 응답 시간이 초과되었습니다."
        rag_job.completed_at = timezone.now()
        rag_job.error_message = f"RAG request timed out after {request_timeout}s"
        rag_job.save(update_fields=["status", "stage", "stage_message", "completed_at", "error_message"])
        logger.error("RAG LLM timeout after %ss: %s", request_timeout, exc)
        return {"status": "failed", "job_id": rag_job_id, "error": rag_job.error_message}
    except Exception as exc:
        rag_job.status = AIStatus.FAILED
        rag_job.stage = RAGStage.FAILED
        rag_job.stage_message = "답변 생성 중 오류가 발생했습니다."
        rag_job.completed_at = timezone.now()
        rag_job.error_message = str(exc)
        rag_job.save(update_fields=["status", "stage", "stage_message", "completed_at", "error_message"])
        logger.exception("RAG LLM error: job_id=%s", rag_job_id)
        return {"status": "failed", "job_id": rag_job_id, "error": str(exc)}
    finally:
        if acquired:
            semaphore.release()
