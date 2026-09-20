from __future__ import annotations

import os
import json

import requests

from capabilities.config import CapabilityConfigError, capability_config_path, load_capability_config
from capabilities.registry import CAPABILITY_REGISTRY

from document_ai.services.embedding_runtime_config import (
    EmbeddingRuntimeConfigError,
    load_embedding_runtime,
)
from document_ai.services.rag_runtime_config import (
    load_llm_runtime_status,
    probe_server_rag_runtime,
    target_from_persisted_config,
)


def build_rag_summary(*, enabled: bool = True) -> dict:
    if not enabled:
        return {
            "configured": False,
            "available": False,
            "status": "disabled",
            "reason_code": "RAG_DISABLED",
            "updated_at": None,
            "model": "",
            "runtime": "",
            "priority_preset": "balanced",
            "selection_mode": "automatic",
            "serving_concurrency": 0,
        }
    target = target_from_persisted_config()
    status = load_llm_runtime_status()
    available = probe_server_rag_runtime(target) if target else False
    serving_profile = target.serving_profile if target else {}
    return {
        "configured": target is not None,
        "available": available,
        "status": ("healthy" if available else "unavailable") if target else "not_configured",
        "reason_code": str(
            status.get("reason_code")
            or ("RUNTIME_HEALTHCHECK_FAILED" if target and not available else "")
        ),
        "updated_at": status.get("updated_at"),
        "model": target.model if target else "",
        "runtime": target.runtime if target else "",
        "priority_preset": target.priority_preset if target else "balanced",
        "selection_mode": target.selection_mode if target else "automatic",
        "serving_concurrency": serving_profile.get("serving_concurrency", 0),
    }


def build_embedding_summary(operation_mode: str, *, probe: bool = False) -> dict:
    from capabilities.registry import DOCUMENT_SEMANTIC_SEARCH
    from document_ai.services.capability_admission import capability_enabled

    _ = operation_mode  # Compatibility parameter; capability config is authoritative.
    enabled = capability_enabled(DOCUMENT_SEMANTIC_SEARCH)
    if not enabled:
        return {
            "enabled": False,
            "configured": False,
            "available": False,
            "status": "disabled",
            "reason_code": "CAPABILITY_DISABLED",
            "model": "",
            "provider": "",
            "dimension": 0,
            "sparse_enabled": False,
            "distance_strategy": "",
        }
    try:
        runtime = load_embedding_runtime()
    except EmbeddingRuntimeConfigError:
        return {
            "enabled": enabled,
            "configured": False,
            "available": False,
            "status": "invalid_config",
            "reason_code": "EMBEDDING_CONFIG_INVALID",
            "model": "",
            "provider": "",
            "dimension": 0,
            "sparse_enabled": False,
            "distance_strategy": "",
        }

    available = enabled
    status_name = "configured" if enabled else "disabled"
    reason_code = ""
    if enabled and probe:
        service_url = os.getenv("EMBEDDING_SERVICE_URL", "http://embedding-executor:8001").rstrip("/")
        try:
            response = requests.get(f"{service_url}/readyz", timeout=2)
            available = response.ok
            status_name = "ready" if response.ok else "not_ready"
            if not response.ok:
                reason_code = "EMBEDDING_READINESS_FAILED"
        except requests.RequestException:
            available = False
            status_name = "unavailable"
            reason_code = "EMBEDDING_SERVICE_UNAVAILABLE"

    return {
        "enabled": enabled,
        "configured": True,
        "available": available,
        "status": status_name,
        "reason_code": reason_code,
        "model": runtime.model_id,
        "provider": runtime.provider,
        "dimension": runtime.dimension,
        "sparse_enabled": runtime.supports_sparse,
        "distance_strategy": runtime.distance_strategy,
    }


def _deployed_services() -> tuple[set[str], str]:
    path = (
        capability_config_path().parent
        / "deployment_scopes"
        / "production"
        / "deployment_plan.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set(), ""
    services = set(payload.get("core_services") or [])
    services.update(
        worker.get("compose_service")
        for worker in payload.get("workers") or []
        if worker.get("enabled") and worker.get("compose_service")
    )
    return services, str(payload.get("capability_revision") or "")


def _deployed_operation_mode() -> str:
    path = (
        capability_config_path().parent
        / "deployment_scopes"
        / "production"
        / "deployment_plan.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return os.environ.get("DOTORI_OPERATION_MODE", "search").strip().lower()
    mode = str(payload.get("mode") or "").strip().lower()
    return mode or os.environ.get("DOTORI_OPERATION_MODE", "search").strip().lower()


def build_capability_summaries(*, probe: bool = False) -> dict[str, dict]:
    try:
        config = load_capability_config()
        config_error = ""
    except CapabilityConfigError as exc:
        config = None
        config_error = str(exc)
    deployed_services, deployed_revision = _deployed_services()
    legacy_deployment = not deployed_revision
    role_services = {"parser": "parser-executor", "embedding": "embedding-executor"}
    summaries = {}
    for capability_id, definition in CAPABILITY_REGISTRY.items():
        enabled = bool(config and config.is_enabled(capability_id))
        required_services = {
            role_services[role]
            for role in definition.execution_roles
            if role in role_services
        }
        installed = enabled and (
            legacy_deployment or required_services.issubset(deployed_services)
        )
        revision_matches = bool(
            config
            and (legacy_deployment or deployed_revision == config.revision)
        )
        available = installed and revision_matches
        status = "disabled"
        reason_code = "CAPABILITY_DISABLED"
        if config_error:
            status = "invalid_config"
            reason_code = "CAPABILITY_CONFIG_INVALID"
            available = False
        elif enabled and not installed:
            status = "not_installed"
            reason_code = "CAPABILITY_NOT_INSTALLED"
        elif enabled and not revision_matches:
            status = "revision_mismatch"
            reason_code = "CAPABILITY_UNAVAILABLE"
        elif enabled:
            status = "configured"
            reason_code = ""
        if probe and available:
            urls = {
                "parser": os.getenv("PARSER_EXECUTOR_URL", "http://parser-executor:8002"),
                "embedding": os.getenv("EMBEDDING_SERVICE_URL", "http://embedding-executor:8001"),
            }
            try:
                available = all(
                    requests.get(f"{urls[role].rstrip('/')}/readyz", timeout=2).ok
                    for role in definition.execution_roles
                    if role in urls
                )
            except requests.RequestException:
                available = False
            status = "ready" if available else "unavailable"
            reason_code = "" if available else "CAPABILITY_UNAVAILABLE"
        summaries[capability_id] = {
            "registered": True,
            "installed": installed,
            "enabled": enabled,
            "available": available,
            "status": status,
            "reason_code": reason_code,
            "contract_version": definition.contract_version,
            "config_revision": config.revision if config else "",
            "deployment_revision": deployed_revision,
            "detail": config_error,
        }
    return summaries


def build_server_policy_payload(*, probe_embedding: bool = False) -> dict:
    operation_mode = _deployed_operation_mode()
    return {
        "operation_mode": operation_mode,
        "rag": build_rag_summary(enabled=operation_mode == "rag"),
        "embedding": build_embedding_summary(operation_mode, probe=probe_embedding),
        "capabilities": build_capability_summaries(probe=probe_embedding),
    }
