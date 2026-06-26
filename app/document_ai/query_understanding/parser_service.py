from __future__ import annotations

import json
import logging
import os
import re
import time as time_module
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta
from uuid import UUID

from django.utils import timezone

from document_ai.parsers.text_utils import normalize_extracted_text

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
            logger.debug("Skipping non-JSON query-parser stream line: %r", line[:200])
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
        "QUERY_PARSER_BASE_URL",
        os.getenv("QUERY_LLM_URL", "http://vllm-query-parser:8080"),
    ).rstrip("/")


def _get_query_parser_model() -> str:
    return os.getenv(
        "QUERY_PARSER_REQUEST_MODEL",
        os.getenv(
            "QUERY_PARSER_SERVED_MODEL_NAME",
            os.getenv(
                "QUERY_PARSER_MODEL",
                os.getenv("QUERY_LLM_MODEL", "meta-llama/Llama-3.2-3B-Instruct"),
            ),
        ),
    )


def _fallback_query_parse(query: str, mode: str, error: Exception | None = None) -> dict:
    from search_engine.query_engine import QueryPipeline

    payload = QueryPipeline(enabled=True).passthrough(query, mode, source="fallback")
    if error is not None:
        payload["status"] = "fallback"
        payload["analysis"]["warnings"].append(
            {
                "code": "query_llm_failed",
                "message": str(error),
            }
        )
    return payload


def _query_dsl_schema_for_prompt() -> dict:
    from search_engine.query_engine.schema import QUERY_DSL_SCHEMA

    return _json_safe(QUERY_DSL_SCHEMA)


def _query_dsl_schema_summary_for_prompt() -> dict:
    from search_engine.query_engine.schema import QUERY_DSL_SCHEMA

    summary = {}
    for scope, config in QUERY_DSL_SCHEMA.items():
        fields = {}
        for field_name, field_config in config.get("fields", {}).items():
            entry = {
                "type": field_config.get("type"),
                "operators": sorted(field_config.get("operators", [])),
            }
            choices = field_config.get("choices")
            if choices:
                entry["choices"] = sorted(choices)
            fields[field_name] = entry
        summary[scope] = {
            "fields": fields,
            "sortable_fields": sorted(config.get("sortable_fields", [])),
        }
    return summary


def _relative_date_rules_summary_for_prompt() -> dict:
    return {
        "today": "Use created_at filters covering the current local day.",
        "yesterday": "Use created_at filters covering the previous local day.",
        "last_7_days": "For expressions like 지난주, last week, 최근 7일 use created_at for the last 7 days.",
        "this_month": "Use created_at filters covering the current local month.",
        "last_month": "Use created_at filters covering the previous local month.",
        "this_year": "Use created_at filters covering the current local year.",
        "last_year": "Use created_at filters covering the previous local year.",
    }


def _passthrough_query_parse(query: str, mode: str, *, source: str, warning: dict | None = None) -> dict:
    from search_engine.query_engine import QueryPipeline

    return QueryPipeline(enabled=True).passthrough(query, mode, source=source, warning=warning)


def _extract_json_object(text: str) -> dict:
    candidate = _extract_llm_final_content(text, fallback_to_raw=True)
    if not candidate:
        raise ValueError("LLM returned empty content.")

    candidate = re.sub(r"^```(?:json)?\s*|\s*```$", "", candidate).strip()

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            try:
                payload = json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                payload = _salvage_partial_query_payload(candidate)
        else:
            payload = _salvage_partial_query_payload(candidate)

    if not isinstance(payload, dict):
        raise ValueError("LLM query parser output must be a JSON object.")
    return payload


def _extract_json_string_field(text: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"((?:\\.|[^"\\])*)"', text)
    if not match:
        return None
    return json.loads(f'"{match.group(1)}"')


def _extract_json_bool_field(text: str, key: str) -> bool | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*(true|false)', text)
    if not match:
        return None
    return match.group(1) == "true"


def _extract_json_number_field(text: str, key: str) -> float | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*(-?\d+(?:\.\d+)?)', text)
    if not match:
        return None
    return float(match.group(1))


def _salvage_partial_query_payload(text: str) -> dict:
    payload = {
        "intent": _extract_json_string_field(text, "intent"),
        "semantic_query": _extract_json_string_field(text, "semantic_query"),
        "retrieval_required": _extract_json_bool_field(text, "retrieval_required"),
        "confidence": _extract_json_number_field(text, "confidence"),
        "Metadata": {
            "filters": [],
            "target_scopes": [],
        },
    }
    if not payload["intent"] or payload["semantic_query"] is None:
        raise ValueError("LLM query parser output must be a JSON object.")
    if payload["retrieval_required"] is None:
        payload["retrieval_required"] = bool(payload["semantic_query"])
    if payload["confidence"] is None:
        payload["confidence"] = 0.0
    return payload


def _build_query_parser_prompt(*, query: str, include_metadata: bool) -> tuple[str, str]:
    if include_metadata:
        system_prompt = (
            "You are a Query Classifier and Meta Data Extractor."
            "Your goal is to analyze the user's query and extract the core query and Metadata."
            "Return only compact JSON. No markdown, no explanations."
            'Shape: {"intent":"", "retrieval_required":true, "confidence":0.0, "semantic_query":"", "Metadata": {"filters": [], "target_scopes": []}}. '
            "Allowed intent values: question, information, app_usage, casual, ambiguous. "
            "Use semantic_query only for content that should be embedded for vector retrieval. "
            "Each filter shape is {scope,field,operator,value,source_text,confidence}. "
            "Use only the provided schema scopes, fields, and operators. "
            "Correct obvious typos in user language, but do not invent metadata filters. "
            "Do not copy schema or input prompt fields into the output."
        )
    else:
        system_prompt = (
            "You are a query classifier for a RAG document search system. "
            "Return only compact JSON. No markdown, no explanations. "
            'Shape: {"intent":"","retrieval_required":true,"confidence":0.0,"semantic_query":""}. '
            "Classify intent based on user query. Allowed intent values: [question, information, app_usage, casual, ambiguous]. "
            "Default to retrieval_required=true when the query asks to find, search, summarize, compare, or answer from documents, files, reports, pages, or uploaded content. "
            "Set retrieval_required=false only for clear casual chat, app usage/help questions, or general world-knowledge questions not about the user's documents. "
            "Short keyword-like queries can still require retrieval. "
            "If the query contains document/file hints, use intent=question and retrieval_required=true. "
            "If unsure, use intent=ambiguous and retrieval_required=true. "
            "semantic_query must contain only the content keywords for vector retrieval. "
            "Remove greetings, dates, file types, upload words, UI commands, and filler words. "
            "Generate semantic_query only from the user's query. "
            "Do not copy schema or prompt fields into the output."
        )

    tz = timezone.get_current_timezone()
    today_dt = datetime.combine(timezone.localdate(), time.min)
    this_month_dt = today_dt.replace(day=1)
    last_month_dt = (this_month_dt - timedelta(days=1)).replace(day=1)
    last_month_start = timezone.make_aware(last_month_dt, tz).isoformat()
    last_month_end = timezone.make_aware(this_month_dt, tz).isoformat()

    prompt_body = {
        "today": timezone.localdate().isoformat(),
        "timezone": str(tz),
        "query": query,
    }
    if include_metadata:
        prompt_body["relative_date_rules_summary"] = _relative_date_rules_summary_for_prompt()
        prompt_body["query_dsl_schema_summary"] = _query_dsl_schema_summary_for_prompt()
        prompt_body["examples"] = [
            {
                "query": "지난 달에 작성된 pdf 문서중에서 회의실 공간 이용 수칙에 대해 요약해줘",
                "output": {
                    "intent": "question",
                    "semantic_query": "회의실 공간 이용 수칙 요약",
                    "retrieval_required": True,
                    "confidence": 0.95,
                    "Metadata": {
                        "filters": [
                            {"scope": "node", "field": "ext", "operator": "eq", "value": "pdf", "source_text": "pdf 문서중에서", "confidence": 1.0},
                            {"scope": "node", "field": "created_at", "operator": "gte", "value": last_month_start, "source_text": "지난 달에", "confidence": 1.0},
                            {"scope": "node", "field": "created_at", "operator": "lt", "value": last_month_end, "source_text": "지난 달에", "confidence": 1.0},
                        ],
                        "target_scopes": ["node"],
                    },
                },
            },
        ]

    return system_prompt, json.dumps(prompt_body, ensure_ascii=False)


def parse_with_llm(query: str, mode: str) -> dict:
    import requests
    from redis import Redis
    from redis_semaphore import NotAvailable, Semaphore
    from search_engine.query_engine import QueryPipeline

    llm_base_url = _get_query_parser_base_url()
    model = _get_query_parser_model()
    redis_url = os.getenv("QUERY_REDIS_URL", os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0"))
    request_timeout = _get_positive_int_env("QUERY_REQUEST_TIMEOUT", 60)
    semaphore_count = _get_positive_int_env("QUERY_SEMAPHORE_COUNT", 1)
    semaphore_timeout = _get_positive_int_env("QUERY_SEMAPHORE_TIMEOUT", request_timeout)
    max_tokens = _get_positive_int_env("QUERY_MAX_TOKENS", 512)
    stale_lock_timeout = max(request_timeout + 60, 300)
    semaphore_namespace = os.getenv(
        "QUERY_SEMAPHORE_NAMESPACE",
        "llm_query_parser_v1",
    )
    pipeline_enabled = os.getenv("QUERY_PIPELINE_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    max_validation_passes = _get_positive_int_env("QUERY_PIPELINE_MAX_VALIDATION_PASSES", 2)
    query_pipeline = QueryPipeline(
        enabled=pipeline_enabled,
        max_validation_passes=max_validation_passes,
    )
    include_metadata = os.getenv("QUERY_PARSER_INCLUDE_METADATA", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }
    system_prompt, user_prompt = _build_query_parser_prompt(query=query, include_metadata=include_metadata)

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
        raise RuntimeError("Query LLM parser is busy. Please retry shortly.") from exc

    try:
        request_started_at = time_module.monotonic()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": float(os.getenv("QUERY_TEMPERATURE", "0.0")),
            "top_p": float(os.getenv("QUERY_TOP_P", "1.0")),
            "max_tokens": max_tokens,
            "stream": True,
            "response_format": {"type": "json_object"},
        }
        reasoning_format = os.getenv("QUERY_REASONING_FORMAT", "").strip()
        if reasoning_format:
            payload["reasoning_format"] = reasoning_format
        response = requests.post(
            f"{llm_base_url}/v1/chat/completions",
            json=payload,
            timeout=(5, request_timeout),
            stream=True,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(f"Query LLM HTTP {response.status_code}: {response.text[:1000]}") from exc
        first_chunk_elapsed_ms = None
        raw_parts = []
        try:
            for content in _iter_openai_responses_stream_content(response):
                if first_chunk_elapsed_ms is None:
                    first_chunk_elapsed_ms = round((time_module.monotonic() - request_started_at) * 1000, 1)
                    logger.info(
                        "Query parser first token received: model=%s elapsed_ms=%s",
                        model,
                        first_chunk_elapsed_ms,
                    )
                raw_parts.append(content)
        except requests.ReadTimeout as exc:
            phase = "before_first_token" if first_chunk_elapsed_ms is None else "after_first_token"
            elapsed_ms = round((time_module.monotonic() - request_started_at) * 1000, 1)
            logger.warning(
                "Query parser stream read timeout: model=%s phase=%s elapsed_ms=%s",
                model,
                phase,
                elapsed_ms,
            )
            raise RuntimeError(
                f"Query parser stream timeout ({phase}) after {elapsed_ms}ms"
            ) from exc
        raw_content = "".join(raw_parts).strip()
        logger.info(
            "Query parser stream completed: model=%s first_token_ms=%s total_ms=%s chars=%s",
            model,
            first_chunk_elapsed_ms,
            round((time_module.monotonic() - request_started_at) * 1000, 1),
            len(raw_content),
        )
        final_content = _extract_llm_final_content(raw_content, fallback_to_raw=True)
        if not final_content:
            return _passthrough_query_parse(
                query,
                mode,
                source="llm_empty_final",
                warning={
                    "code": "query_llm_empty_final",
                    "message": "Query LLM returned no final content. Original query was used as semantic query.",
                },
            )
        try:
            raw_dsl_payload = _extract_json_object(final_content)
        except Exception as exc:
            raise RuntimeError(f"Query LLM returned invalid JSON: {raw_content[:1000]!r}") from exc

        result = query_pipeline.run(query, mode, raw_dsl_payload)
        result["debug"].update(
            {
                "llm_model": model,
                "llm_url": llm_base_url,
            }
        )
        return result
    finally:
        if acquired:
            semaphore.release()


def parse_user_query_sync(query: str, mode: str = "search") -> dict:
    normalized_query = normalize_extracted_text(query or "").strip()
    if not normalized_query:
        return _fallback_query_parse(query, mode)

    llm_enabled = os.getenv("QUERY_LLM_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    if not llm_enabled:
        return _passthrough_query_parse(
            normalized_query,
            mode,
            source="llm_disabled",
            warning={
                "code": "query_llm_disabled",
                "message": "QUERY_LLM_ENABLED is disabled. Original query was used as semantic query.",
            },
        )

    try:
        return parse_with_llm(normalized_query, mode)
    except Exception as exc:
        logger.warning("Query LLM parser failed; preserving original query: %s", exc)
        return _fallback_query_parse(query, mode, error=exc)
