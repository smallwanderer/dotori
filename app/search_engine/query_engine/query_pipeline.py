from __future__ import annotations

import os
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from uuid import UUID

from document_ai.parsers.text_utils import normalize_extracted_text
from config.enums import QueryAnswerMode, QueryIntent

from search_engine.query_engine.contracts import DSLFilter, DSLSort, QueryDSL
from search_engine.query_engine.schema import QUERY_DSL_SCHEMA
from search_engine.query_engine.translators import ORMCompiler
from search_engine.query_engine.validators import QueryDSLValidator


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


class QueryPipeline:
    """
    LLM이 추출한 제한 QueryDSL 후보를 시스템 스키마 기준으로 검증하고 ORM kwargs로 컴파일한다.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        schema: dict | None = None,
        max_validation_passes: int = 2,
    ):
        self.enabled = enabled
        self.schema = schema or QUERY_DSL_SCHEMA
        self.max_validation_passes = max(1, max_validation_passes)
        self.validator = QueryDSLValidator(self.schema, strict=False)
        self.compiler = ORMCompiler()

    def run(self, raw_query: str, mode: str, llm_payload: dict, *, source: str = "llm_query_pipeline") -> dict:
        if not self.enabled:
            return self.passthrough(
                raw_query,
                mode,
                source="query_pipeline_disabled",
                warning={
                    "code": "query_pipeline_disabled",
                    "message": "QUERY_PIPELINE_ENABLED is disabled. Original query was used as semantic query.",
                },
            )

        normalized_query = normalize_extracted_text(raw_query or "").strip()
        normalized_payload = self._normalize_payload(llm_payload)
        dsl = self._payload_to_dsl(normalized_payload, fallback_query=normalized_query)
        validation_passes = []
        validated = None
        current = dsl
        collected_issues = []

        for pass_index in range(self.max_validation_passes):
            validated = self.validator.validate(current)
            issues = _json_safe(validated.issues)
            validation_passes.append(
                {
                    "pass": pass_index + 1,
                    "valid": validated.valid,
                    "issues": issues,
                    "dsl": _json_safe(validated.dsl),
                }
            )
            collected_issues.extend(issues)
            if not issues:
                break
            current = validated.dsl

        if validated is None:
            validated = self.validator.validate(dsl)

        filter_kwargs, exclude_kwargs, order_by = self.compiler.compile_dsl(validated.dsl, self.schema)
        unique_issues = self._dedupe_issues(collected_issues)
        semantic_query = validated.dsl.semantic_query
        classification = self._build_classification(normalized_payload, mode=mode, semantic_query=semantic_query)
        if not semantic_query and classification["retrieval_required"]:
            semantic_query = normalized_query
        question = self._build_question(raw_query, normalized_query, semantic_query)

        return {
            "status": "partial" if unique_issues else "success",
            "mode": mode,
            "source": source,
            "raw_query": raw_query or "",
            "normalized_query": normalized_query,
            "is_empty": not bool(normalized_query),
            "intent": classification["intent"],
            "answer_mode": classification["answer_mode"],
            "retrieval_required": classification["retrieval_required"],
            "confidence": classification["confidence"],
            "classification": classification,
            "analysis": {
                "primary_locale": "ko",
                "active_locales": ["ko"],
                "warnings": [
                    {"code": "query_dsl_validation", "message": issue["message"]}
                    for issue in unique_issues
                    if issue.get("level") != "error"
                ],
            },
            "semantic_query": semantic_query,
            "question": question,
            "metadata": {
                "filters": _json_safe(validated.dsl.filters),
                "sorts": _json_safe(validated.dsl.sorts),
                "target_scopes": _json_safe(validated.dsl.target_scopes),
            },
            "dsl": _json_safe(validated.dsl),
            "orm": {
                "base_model": self.schema.get("node", {}).get("model", "files.Node"),
                "filter_kwargs": _json_safe(filter_kwargs),
                "exclude_kwargs": _json_safe(exclude_kwargs),
                "order_by": _json_safe(order_by),
            },
            "validation": {
                "valid": validated.valid,
                "issues": unique_issues,
                "passes": validation_passes,
            },
            "debug": {
                "raw_llm_payload": _json_safe(llm_payload),
                "normalized_llm_payload": _json_safe(normalized_payload),
                "max_validation_passes": self.max_validation_passes,
            },
        }

    def passthrough(self, raw_query: str, mode: str, *, source: str, warning: dict | None = None) -> dict:
        normalized_query = normalize_extracted_text(raw_query or "").strip()
        question = self._build_question(raw_query, normalized_query, normalized_query)
        warnings = [warning] if warning else []
        classification = self._default_classification(mode=mode, semantic_query=normalized_query)
        return {
            "status": "fallback" if warning else "success",
            "mode": mode,
            "source": source,
            "raw_query": raw_query or "",
            "normalized_query": normalized_query,
            "is_empty": not bool(normalized_query),
            "intent": classification["intent"],
            "answer_mode": classification["answer_mode"],
            "retrieval_required": classification["retrieval_required"],
            "confidence": classification["confidence"],
            "classification": classification,
            "analysis": {"primary_locale": "ko", "active_locales": ["ko"], "warnings": warnings},
            "semantic_query": normalized_query,
            "question": question,
            "metadata": {"filters": [], "sorts": [], "target_scopes": []},
            "dsl": {"semantic_query": normalized_query, "filters": [], "sorts": [], "target_scopes": []},
            "orm": {
                "base_model": self.schema.get("node", {}).get("model", "files.Node"),
                "filter_kwargs": {},
                "exclude_kwargs": {},
                "order_by": [],
            },
            "validation": {"valid": True, "issues": [], "passes": []},
            "debug": {"passthrough": True},
        }

    def _payload_to_dsl(self, payload: dict, *, fallback_query: str) -> QueryDSL:
        filters = []
        for item in payload.get("filters") or []:
            if not isinstance(item, dict):
                continue
            filters.append(
                DSLFilter(
                    scope=str(item.get("scope") or "node"),
                    field=str(item.get("field") or ""),
                    operator=str(item.get("operator") or "eq"),
                    value=item.get("value"),
                    source_text=str(item.get("source_text") or ""),
                    confidence=self._confidence(item.get("confidence")),
                )
            )

        sorts = []
        for item in payload.get("sorts") or []:
            if not isinstance(item, dict):
                continue
            sorts.append(
                DSLSort(
                    scope=str(item.get("scope") or "node"),
                    field=str(item.get("field") or ""),
                    direction=str(item.get("direction") or "desc"),
                    source_text=str(item.get("source_text") or ""),
                )
            )

        target_scopes = [
            str(scope)
            for scope in (payload.get("target_scopes") or [])
            if isinstance(scope, str) and scope.strip()
        ]
        if "semantic_query" in payload:
            semantic_query = str(payload.get("semantic_query") or "").strip()
        elif "query" in payload:
            semantic_query = str(payload.get("query") or "").strip()
        else:
            semantic_query = str(fallback_query or "").strip()

        return QueryDSL(
            semantic_query=semantic_query,
            filters=filters,
            sorts=sorts,
            target_scopes=target_scopes,
        )

    def _normalize_payload(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {}

        normalized = dict(payload)
        metadata = normalized.get("Metadata")
        if not isinstance(metadata, dict):
            metadata = normalized.get("metadata")
        if isinstance(metadata, dict):
            if "filters" not in normalized and isinstance(metadata.get("filters"), list):
                normalized["filters"] = metadata.get("filters")
            if "sorts" not in normalized and isinstance(metadata.get("sorts"), list):
                normalized["sorts"] = metadata.get("sorts")
            if "target_scopes" not in normalized and isinstance(metadata.get("target_scopes"), list):
                normalized["target_scopes"] = metadata.get("target_scopes")

        normalized["intent"] = self._normalize_intent_value(normalized.get("intent"))
        normalized["answer_mode"] = self._normalize_answer_mode_value(
            normalized.get("answer_mode"),
            normalized.get("intent"),
            normalized.get("retrieval_required"),
            normalized.get("confidence"),
        )
        return normalized

    def _confidence(self, value) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 1.0

    def _build_question(self, raw_query: str, normalized_query: str, semantic_query: str) -> dict:
        query = semantic_query or normalized_query or raw_query or ""
        return {
            "original": raw_query or "",
            "normalized": normalized_query or "",
            "semantic": query,
            "residual": query,
            "dense": query,
            "sparse": query,
        }

    def _build_classification(self, payload: dict, *, mode: str, semantic_query: str) -> dict:
        default = self._default_classification(mode=mode, semantic_query=semantic_query)
        raw_classification = payload.get("classification")
        if not isinstance(raw_classification, dict):
            raw_classification = {}

        retrieval_required = self._bool(
            payload.get("retrieval_required", raw_classification.get("retrieval_required")),
            default["retrieval_required"],
        )
        confidence = self._confidence(payload.get("confidence", raw_classification.get("confidence", 0.0)))
        intent = self._choice(
            payload.get("intent") or raw_classification.get("intent"),
            {choice.value for choice in QueryIntent},
            default["intent"],
        )
        answer_mode = self._normalize_answer_mode_value(
            payload.get("answer_mode") or raw_classification.get("answer_mode"),
            intent,
            retrieval_required,
            confidence,
        )
        reason = str(payload.get("reason") or raw_classification.get("reason") or "").strip()

        return {
            "intent": intent,
            "answer_mode": answer_mode,
            "retrieval_required": retrieval_required,
            "confidence": confidence,
            "reason": reason[:255],
        }

    def _normalize_intent_value(self, value) -> str:
        candidate = str(value or "").strip().lower()
        aliases = {
            "question": QueryIntent.DOCUMENT_QUESTION.value,
            "document_query": QueryIntent.DOCUMENT_QUESTION.value,
            "information": QueryIntent.INFORMATION.value,
            "document": QueryIntent.INFORMATION.value,
            "casual": QueryIntent.CASUAL_CHAT.value,
            "casual_chat": QueryIntent.CASUAL_CHAT.value,
            "chat": QueryIntent.CASUAL_CHAT.value,
            "app": QueryIntent.APP_USAGE.value,
            "app_usage": QueryIntent.APP_USAGE.value,
            "help": QueryIntent.APP_USAGE.value,
            "how": QueryIntent.APP_USAGE.value,
            "using": QueryIntent.APP_USAGE.value,
        }
        return aliases.get(candidate, candidate)

    def _normalize_answer_mode_value(self, value, intent_value, retrieval_required, confidence=None) -> str:
        candidate = str(value or "").strip().lower()
        if candidate and candidate in {choice.value for choice in QueryAnswerMode}:
            return candidate

        intent = self._normalize_intent_value(intent_value)
        confidence_value = self._confidence(confidence)
        direct_answer_threshold = self._confidence_direct_answer_threshold()

        retrieval_enabled = self._bool(retrieval_required, False)

        if not retrieval_enabled and confidence_value < direct_answer_threshold:
            return QueryAnswerMode.AMBIGUOUS.value
        if retrieval_enabled:
            return QueryAnswerMode.RAG.value
        if intent == QueryIntent.CASUAL_CHAT.value:
            return QueryAnswerMode.CASUAL.value
        if intent == QueryIntent.APP_USAGE.value:
            return QueryAnswerMode.APP_HELP.value
        if intent == QueryIntent.INFORMATION.value:
            return QueryAnswerMode.GENERAL.value
        return QueryAnswerMode.AMBIGUOUS.value

    def _confidence_direct_answer_threshold(self) -> float:
        try:
            return max(0.0, min(1.0, float(os.getenv("QUERY_CONFIDENCE_DIRECT_ANSWER_THRESHOLD", "0.7"))))
        except (TypeError, ValueError):
            return 0.7

    def _default_classification(self, *, mode: str, semantic_query: str) -> dict:
        return {
            "intent": QueryIntent.DOCUMENT_QUESTION.value,
            "answer_mode": QueryAnswerMode.RAG.value if mode == "rag" else QueryAnswerMode.GENERAL.value,
            "retrieval_required": bool(semantic_query),
            "confidence": 0.0,
            "reason": "",
        }

    def _choice(self, value, allowed: set[str], fallback: str) -> str:
        candidate = str(value or "").strip()
        return candidate if candidate in allowed else fallback

    def _bool(self, value, fallback: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "y", "on"}:
                return True
            if lowered in {"0", "false", "no", "n", "off"}:
                return False
        return fallback

    def _dedupe_issues(self, issues: list[dict]) -> list[dict]:
        seen = set()
        deduped = []
        for issue in issues:
            key = (
                issue.get("code"),
                issue.get("path"),
                issue.get("message"),
                issue.get("level"),
            )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(issue)
        return deduped
