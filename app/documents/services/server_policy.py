from __future__ import annotations

import os

import requests

from capabilities.deployment import DeploymentSnapshotError, load_deployment_snapshot
from capabilities.registry import CAPABILITY_REGISTRY

from documents.services.embedding_runtime_config import (
    EmbeddingRuntimeConfigError,
    load_embedding_runtime,
)
from llm_installation.rag_runtime_config import (
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
        "selection_mode": target.selection_mode if target else "automatic",
        "serving_concurrency": serving_profile.get("serving_concurrency", 0),
    }


def build_embedding_summary(operation_mode: str, *, probe: bool = False) -> dict:
    from capabilities.registry import DOCUMENT_SEMANTIC_SEARCH
    from documents.services.capability_admission import capability_enabled

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
    try:
        snapshot = load_deployment_snapshot()
    except DeploymentSnapshotError:
        return set(), ""
    return set(snapshot.services), snapshot.capability_revision


def _deployed_operation_mode() -> str:
    try:
        snapshot = load_deployment_snapshot()
    except DeploymentSnapshotError:
        return os.environ.get("DOTORI_OPERATION_MODE", "search").strip().lower()
    if not snapshot.selected_modules:
        return "basic"
    if snapshot.provider_enabled("llm"):
        return "rag"
    if snapshot.provider_enabled("embedding"):
        return "search"
    return "extract"


def build_capability_summaries(*, probe: bool = False) -> dict[str, dict]:
    from capabilities.execution import executor_url, get_execution_role
    try:
        snapshot = load_deployment_snapshot()
        config_error = ""
    except DeploymentSnapshotError as exc:
        snapshot = None
        config_error = str(exc)
    deployed_services, deployed_revision = _deployed_services()
    summaries = {}
    for capability_id, definition in CAPABILITY_REGISTRY.items():
        installed_in_plan = bool(snapshot and snapshot.is_installed(capability_id))
        enabled = bool(snapshot and snapshot.is_enabled(capability_id))
        required_services = {
            service
            for role in definition.execution_roles
            if (service := get_execution_role(role).compose_service)
        }
        installed = installed_in_plan and required_services.issubset(deployed_services)
        available = enabled and installed
        status = "disabled"
        reason_code = "CAPABILITY_DISABLED"
        if config_error:
            status = "invalid_config"
            reason_code = "CAPABILITY_CONFIG_INVALID"
            available = False
        elif installed_in_plan and not installed:
            status = "not_installed"
            reason_code = "CAPABILITY_NOT_INSTALLED"
        elif enabled:
            status = "configured"
            reason_code = ""
        if probe and available:
            try:
                available = all(
                    requests.get(f"{executor_url(role)}/readyz", timeout=2).ok
                    for role in definition.execution_roles
                )
            except (requests.RequestException, ValueError):
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
            "config_revision": snapshot.capability_revision if snapshot else "",
            "deployment_revision": deployed_revision,
            "detail": config_error,
        }
    return summaries


def build_server_policy_payload(*, probe_embedding: bool = False) -> dict:
    operation_mode = _deployed_operation_mode()
    try:
        rag_enabled = load_deployment_snapshot().provider_enabled("llm")
    except DeploymentSnapshotError:
        rag_enabled = False
    return {
        "operation_mode": operation_mode,
        "rag": build_rag_summary(enabled=rag_enabled),
        "embedding": build_embedding_summary(operation_mode, probe=probe_embedding),
        "capabilities": build_capability_summaries(probe=probe_embedding),
    }
