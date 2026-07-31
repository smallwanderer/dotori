from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from llm_installation.runtime_probe import EndpointStatus, SmokeTestStatus


def get_llm_runtime_config_path(
    scope: str | None = None, *, repo_root: Path | None = None
) -> Path:
    configured_path = os.getenv("LLM_RUNTIME_CONFIG_PATH", "").strip()
    if configured_path:
        return Path(configured_path)
    resolved_scope = scope or os.getenv("RUNTIME_SCOPE", "production")
    root = repo_root or Path(__file__).resolve().parents[3]
    return root / "data" / "config" / "runtime_scopes" / resolved_scope / "llm_runtime.json"


def load_llm_runtime_config(
    path: Path | None = None, scope: str | None = None, *, repo_root: Path | None = None
) -> dict[str, Any]:
    config_path = path or get_llm_runtime_config_path(scope, repo_root=repo_root)
    if not config_path.exists():
        return {}

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}
    return payload


def get_llm_runtime_status_path(
    scope: str | None = None, *, repo_root: Path | None = None
) -> Path:
    configured_path = os.getenv("LLM_RUNTIME_STATUS_PATH", "").strip()
    if configured_path:
        return Path(configured_path)
    configured_runtime = os.getenv("LLM_RUNTIME_CONFIG_PATH", "").strip()
    if configured_runtime:
        return Path(configured_runtime).with_name("runtime_status.json")
    resolved_scope = scope or os.getenv("RUNTIME_SCOPE", "production")
    root = repo_root or Path(__file__).resolve().parents[3]
    return (
        root
        / "data"
        / "config"
        / "runtime_scopes"
        / resolved_scope
        / "runtime_status.json"
    )


def load_llm_runtime_status(
    path: Path | None = None,
    scope: str | None = None,
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    status_path = path or get_llm_runtime_status_path(scope, repo_root=repo_root)
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def server_rag_runtime_availability() -> tuple[bool, dict[str, Any]]:
    """Return persisted local-runtime readiness without doing network I/O.

    Missing status is accepted for compatibility with installs created before
    runtime status persistence was introduced. Once a status file exists,
    only a validated healthy runtime may receive new server-local RAG work.
    """
    runtime_status = load_llm_runtime_status()
    if not runtime_status:
        return True, {}
    return runtime_status.get("status") == "healthy", runtime_status


@dataclass(frozen=True)
class ServerRAGTarget:
    endpoint_name: str
    base_url: str
    model: str
    runtime: str
    reason: str
    fallback_used: bool = False
    endpoint_status: EndpointStatus | None = None
    health_status: EndpointStatus | None = None
    smoke_status: SmokeTestStatus | None = None
    diagnostics: dict[str, Any] | None = None
    priority_preset: str = "balanced"
    selection_mode: str = "automatic"
    selection_reason_code: str = ""
    runtime_policy_input: dict[str, Any] | None = None
    serving_profile: dict[str, Any] | None = None

    def as_snapshot(self) -> dict:
        return {
            "llm_endpoint_name": self.endpoint_name,
            "llm_base_url": self.base_url,
            "llm_model": self.model,
        }


class LLMRuntimeNotConfigured(RuntimeError):
    """Raised when request-time code cannot load an installed runtime config."""


def target_from_persisted_config() -> ServerRAGTarget | None:
    config = load_llm_runtime_config()
    target = config.get("target") if isinstance(config, dict) else None
    if not isinstance(target, dict):
        return None

    base_url = (target.get("base_url") or "").strip().rstrip("/")
    model = (target.get("model") or "").strip()
    if not base_url or not model:
        return None
    if base_url.endswith("/v1"):
        base_url = base_url[:-3].rstrip("/")

    return ServerRAGTarget(
        endpoint_name=target.get("endpoint_name") or "Server configured",
        base_url=base_url,
        model=model,
        runtime=target.get("runtime") or "unknown",
        reason=target.get("reason") or "Loaded from persisted LLM runtime config.",
        fallback_used=bool(target.get("fallback_used", False)),
        diagnostics=config.get("diagnostics") if isinstance(config.get("diagnostics"), dict) else None,
        priority_preset=target.get("priority_preset") or "balanced",
        selection_mode=target.get("selection_mode") or "automatic",
        selection_reason_code=target.get("selection_reason_code") or "",
        runtime_policy_input=(
            target.get("runtime_policy_input")
            if isinstance(target.get("runtime_policy_input"), dict)
            else None
        ),
        serving_profile=target.get("serving_profile") if isinstance(target.get("serving_profile"), dict) else None,
    )


def get_configured_server_rag_target() -> ServerRAGTarget:
    target = target_from_persisted_config()
    if target:
        return target
    raise LLMRuntimeNotConfigured(
        "No valid persisted LLM runtime config was found. Run "
        "`python manage.py detect_llm_runtime --write` during installation "
        "or after an operator-triggered runtime change."
    )


@lru_cache(maxsize=1)
def get_cached_server_rag_target() -> ServerRAGTarget:
    return get_configured_server_rag_target()


def clear_server_rag_target_cache() -> None:
    get_cached_server_rag_target.cache_clear()
