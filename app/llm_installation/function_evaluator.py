"""Small online contract probes for explicitly selected model functions."""

from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from urllib.parse import urlsplit

import requests

from llm_installation.function_catalog import (
    FunctionEvidenceRecord,
    FunctionEvidenceTarget,
)


TEXT_GENERATE_CONTRACT_VERSION = "text-generate-v1"
TEXT_GENERATE_EVALUATOR_VERSION = "dotori-text-generate-contract-v2"
TEXT_GENERATE_FIXTURE_ID = "dotori-text-generate-smoke"
TEXT_GENERATE_FIXTURE_REVISION = "v1"
TEXT_GENERATE_PROMPT = "Return exactly this token and nothing else: DOTORI_OK"
_SUCCESS_TOKEN = re.compile(r"\bDOTORI_OK\b")


@dataclass(frozen=True)
class TextGenerationEvaluationResult:
    evaluation_id: str
    passed: bool
    checks: dict[str, bool]
    failures: tuple[str, ...]
    metrics: dict[str, float]
    evidence: FunctionEvidenceRecord | None

    def as_dict(self) -> dict:
        return {
            "evaluation_id": self.evaluation_id,
            "function_id": "text.generate",
            "passed": self.passed,
            "checks": self.checks,
            "failures": list(self.failures),
            "metrics": self.metrics,
            "evidence": self.evidence.model_dump(mode="json") if self.evidence else None,
        }


def _normalize_base_url(base_url: str) -> str:
    value = (base_url or "").strip().rstrip("/")
    if value.endswith("/v1"):
        value = value[:-3].rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url must be an HTTP(S) origin without credentials or query parameters")
    return value


def _completion_content(payload: object) -> tuple[bool, str]:
    if not isinstance(payload, dict):
        return False, ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False, ""
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return False, ""
    content = message.get("content")
    return (True, content) if isinstance(content, str) else (False, "")


def _stream_text(response) -> tuple[bool, bool, str, int]:
    saw_done = False
    valid_frames = True
    pieces: list[str] = []
    frame_count = 0
    for raw_line in response.iter_lines(decode_unicode=True):
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = str(raw_line or "")
        line = line.strip()
        if not line or not line.startswith("data:"):
            continue
        frame = line[5:].strip()
        if frame == "[DONE]":
            saw_done = True
            continue
        try:
            payload = json.loads(frame)
        except (TypeError, ValueError):
            valid_frames = False
            continue
        if not isinstance(payload, dict):
            valid_frames = False
            continue
        choices = payload.get("choices")
        if not isinstance(choices, list):
            valid_frames = False
            continue
        frame_count += 1
        if choices and isinstance(choices[0], dict):
            delta = choices[0].get("delta")
            content = delta.get("content") if isinstance(delta, dict) else None
            if isinstance(content, str):
                pieces.append(content)
    return valid_frames, saw_done, "".join(pieces), frame_count


def evaluate_text_generate_contract(
    *,
    base_url: str,
    model: str,
    model_id: str,
    model_revision: str,
    artifact_id: str,
    artifact_revision: str,
    artifact_digest: str,
    backend_id: str,
    backend_version: str,
    adapter_contract: str,
    evaluation_id: str,
    timeout: float = 20,
) -> TextGenerationEvaluationResult:
    """Probe non-stream and stream chat-completion contracts with a synthetic prompt.

    The result contains no endpoint URL, response body, or credential. A passing
    run includes contract evidence; a failed run remains a report and can never
    be loaded as verified catalog evidence.
    """
    normalized_base_url = _normalize_base_url(base_url)
    required_values = {
        "model": model,
        "model_id": model_id,
        "model_revision": model_revision,
        "artifact_id": artifact_id,
        "artifact_revision": artifact_revision,
        "artifact_digest": artifact_digest,
        "backend_id": backend_id,
        "backend_version": backend_version,
        "adapter_contract": adapter_contract,
        "evaluation_id": evaluation_id,
    }
    missing = [name for name, value in required_values.items() if not str(value or "").strip()]
    if missing:
        raise ValueError(f"Missing evaluation identity fields: {', '.join(missing)}")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", artifact_digest):
        raise ValueError("artifact_digest must be a SHA-256 hex digest")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", evaluation_id):
        raise ValueError("evaluation_id must be a safe identifier")
    bounded_timeout = max(1.0, min(float(timeout), 120.0))

    checks = {
        "nonstream_http_ok": False,
        "nonstream_response_schema": False,
        "nonstream_fixture_match": False,
        "stream_http_ok": False,
        "stream_frames_valid": False,
        "stream_done_received": False,
        "stream_fixture_match": False,
    }
    failures: list[str] = []
    metrics: dict[str, float] = {}
    endpoint = f"{normalized_base_url}/v1/chat/completions"
    prompt_started = monotonic()
    response = None
    try:
        response = requests.post(
            endpoint,
            json={
                "model": model,
                "messages": [{"role": "user", "content": TEXT_GENERATE_PROMPT}],
                "max_tokens": 16,
                "temperature": 0,
                "stream": False,
            },
            timeout=bounded_timeout,
        )
        checks["nonstream_http_ok"] = 200 <= response.status_code < 300
        if checks["nonstream_http_ok"]:
            try:
                payload = response.json()
            except (TypeError, ValueError):
                payload = None
            schema_valid, content = _completion_content(payload)
            checks["nonstream_response_schema"] = schema_valid
            checks["nonstream_fixture_match"] = schema_valid and bool(
                _SUCCESS_TOKEN.search(content)
            )
        else:
            failures.append(f"nonstream_http_{response.status_code}")
    except requests.RequestException as exc:
        failures.append(f"nonstream_{type(exc).__name__}")
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    metrics["nonstream_elapsed_ms"] = round((monotonic() - prompt_started) * 1000, 3)

    stream_started = monotonic()
    response = None
    try:
        response = requests.post(
            endpoint,
            json={
                "model": model,
                "messages": [{"role": "user", "content": TEXT_GENERATE_PROMPT}],
                "max_tokens": 16,
                "temperature": 0,
                "stream": True,
            },
            timeout=bounded_timeout,
            stream=True,
        )
        checks["stream_http_ok"] = 200 <= response.status_code < 300
        if checks["stream_http_ok"]:
            frames_valid, done_received, content, frame_count = _stream_text(response)
            checks["stream_frames_valid"] = frames_valid and frame_count > 0
            checks["stream_done_received"] = done_received
            checks["stream_fixture_match"] = bool(_SUCCESS_TOKEN.search(content))
            metrics["stream_frame_count"] = float(frame_count)
        else:
            failures.append(f"stream_http_{response.status_code}")
    except requests.RequestException as exc:
        failures.append(f"stream_{type(exc).__name__}")
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    metrics["stream_elapsed_ms"] = round((monotonic() - stream_started) * 1000, 3)

    for name, passed in checks.items():
        if not passed:
            failures.append(name)
    failures = list(dict.fromkeys(failures))
    passed = not failures
    evidence = None
    if passed:
        evidence = FunctionEvidenceRecord.model_validate(
            {
                "schema_version": 1,
                "evidence_id": evaluation_id,
                "function_id": "text.generate",
                "verification": "contract_verified",
                "model_id": model_id,
                "model_revision": model_revision,
                "contract_version": TEXT_GENERATE_CONTRACT_VERSION,
                "targets": [
                    {
                        "target_id": artifact_id,
                        "target_revision": artifact_revision,
                        "artifact_digest": artifact_digest.lower(),
                        "backend_id": backend_id,
                        "backend_version": backend_version,
                        "adapter_contract": adapter_contract,
                    }
                ],
                "input_preprocessing_version": "openai-chat-messages-v1",
                "output_parsing_version": "openai-chat-completion-sse-v1",
                "inference_options": {
                    "max_tokens": 16,
                    "temperature": 0,
                    "requests": ["nonstream", "stream"],
                    "request_timeout_seconds": bounded_timeout,
                },
                "fixture_set_id": TEXT_GENERATE_FIXTURE_ID,
                "fixture_set_revision": TEXT_GENERATE_FIXTURE_REVISION,
                "evaluator_version": TEXT_GENERATE_EVALUATOR_VERSION,
                "executed_at": datetime.now(timezone.utc).isoformat(),
                "environment": {
                    "platform": platform.platform(),
                    "python_version": platform.python_version(),
                },
                "sample_count": 2,
                "contract_checks": checks,
                "metrics": metrics,
                "failures": [],
                "passed": True,
            }
        )
    return TextGenerationEvaluationResult(
        evaluation_id=evaluation_id,
        passed=passed,
        checks=checks,
        failures=tuple(failures),
        metrics=metrics,
        evidence=evidence,
    )
