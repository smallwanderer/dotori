from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llm_installation.runtime_lifecycle import RuntimeSpec


SCOPES = ("production",)
MODES = ("rag", "search", "basic")
NETWORK_ACCESS_MODES = ("local", "direct_https")
CORE_SERVICES = ("db", "redis", "app")
ALL_WORKER_SERVICES = ("dotori-document",)


def compose_up_command(
    compose_command: str,
    services: tuple[str, ...] | list[str],
    *,
    build_images: bool,
    force_recreate: bool = False,
) -> str:
    """Build the explicit fast-start or maintenance rebuild command."""
    build_flag = "--build" if build_images else "--no-build"
    parts = [compose_command, "up", build_flag, "--remove-orphans"]
    if force_recreate:
        parts.append("--force-recreate")
    parts.extend(["-d", *services])
    return " ".join(parts)


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    compose_service: str
    queues: tuple[str, ...]
    concurrency: int
    prefetch_multiplier: int
    enabled: bool
    requires_runtime: bool
    dependencies: tuple[str, ...]
    health_strategy: str

    def __post_init__(self) -> None:
        if not self.name or not self.compose_service:
            raise ValueError("Worker name and Compose service are required.")
        if self.concurrency < 1:
            raise ValueError("Worker concurrency must be at least 1.")
        if self.prefetch_multiplier < 1:
            raise ValueError("Worker prefetch multiplier must be at least 1.")
        if not self.queues and self.health_strategy != "celery-beat":
            raise ValueError("A queue worker must consume at least one queue.")

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DeploymentPlan:
    scope: str
    mode: str
    core_services: tuple[str, ...]
    workers: tuple[WorkerSpec, ...]
    runtime: RuntimeSpec | None
    network_access: str
    generation_id: str

    def __post_init__(self) -> None:
        if self.scope not in SCOPES:
            raise ValueError(f"Unknown deployment scope: {self.scope}")
        if self.mode not in MODES:
            raise ValueError(f"Unknown deployment mode: {self.mode}")
        if self.network_access not in NETWORK_ACCESS_MODES:
            raise ValueError(f"Unknown network access mode: {self.network_access}")
        if tuple(self.core_services) != CORE_SERVICES:
            raise ValueError(f"Core services must be {CORE_SERVICES!r}.")
        if self.runtime is not None and self.runtime.scope != self.scope:
            raise ValueError("RuntimeSpec scope must match DeploymentPlan scope.")
        if self.mode != "rag" and self.runtime is not None:
            raise ValueError("Only RAG mode may include a local RuntimeSpec.")
        if any(worker.requires_runtime and worker.enabled for worker in self.workers):
            if self.runtime is None:
                raise ValueError("A runtime-dependent worker requires a RuntimeSpec.")

    @property
    def enabled_workers(self) -> tuple[WorkerSpec, ...]:
        return tuple(worker for worker in self.workers if worker.enabled)

    @property
    def enabled_services(self) -> tuple[str, ...]:
        return self.core_services + tuple(
            worker.compose_service for worker in self.enabled_workers
        )

    @property
    def disabled_worker_services(self) -> tuple[str, ...]:
        return tuple(
            worker.compose_service for worker in self.workers if not worker.enabled
        )

    def as_dict(self) -> dict:
        runtime = None
        if self.runtime is not None:
            runtime = asdict(self.runtime)
            runtime["args_file"] = str(runtime["args_file"])
        return {
            "scope": self.scope,
            "mode": self.mode,
            "core_services": list(self.core_services),
            "workers": [worker.as_dict() for worker in self.workers],
            "runtime": runtime,
            "network_access": self.network_access,
            "generation_id": self.generation_id,
        }


def normalize_mode(mode: str) -> str:
    aliases = {
        "1": "rag",
        "2": "search",
        "3": "basic",
        "full": "rag",
        "full-rag": "rag",
    }
    normalized = aliases.get(str(mode).strip().lower(), str(mode).strip().lower())
    if normalized not in MODES:
        raise ValueError(f"Unknown deployment mode: {mode}")
    return normalized


def _worker_specs(mode: str, *, runtime_available: bool) -> tuple[WorkerSpec, ...]:
    ai_enabled = mode in {"rag", "search"}
    return (
        WorkerSpec(
            name="embedding",
            compose_service="dotori-document",
            queues=("parse", "embed"),
            concurrency=1,
            prefetch_multiplier=1,
            enabled=ai_enabled,
            requires_runtime=False,
            dependencies=("db", "redis"),
            health_strategy="celery-ping",
        ),
    )


def _generation_id(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"plan-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"


def build_deployment_plan(
    mode: str,
    *,
    scope: str = "production",
    runtime: RuntimeSpec | None = None,
    network_access: str = "local",
) -> DeploymentPlan:
    normalized_mode = normalize_mode(mode)
    workers = _worker_specs(normalized_mode, runtime_available=runtime is not None)
    unsigned = {
        "scope": scope,
        "mode": normalized_mode,
        "core_services": CORE_SERVICES,
        "workers": [worker.as_dict() for worker in workers],
        "runtime_generation": runtime.generation_id if runtime else None,
        "network_access": network_access,
    }
    return DeploymentPlan(
        scope=scope,
        mode=normalized_mode,
        core_services=CORE_SERVICES,
        workers=workers,
        runtime=runtime,
        network_access=network_access,
        generation_id=_generation_id(unsigned),
    )


def deployment_plan_path(
    scope: str, *, repo_root: Path | None = None
) -> Path:
    if scope not in SCOPES:
        raise ValueError(f"Unknown deployment scope: {scope}")
    root = repo_root or Path(__file__).resolve().parents[1]
    return root / "data" / "config" / "deployment_scopes" / scope / "deployment_plan.json"


def write_deployment_plan(
    plan: DeploymentPlan, *, repo_root: Path | None = None
) -> Path:
    path = deployment_plan_path(plan.scope, repo_root=repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(plan.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)
    return path


def read_deployment_plan(
    scope: str, *, repo_root: Path | None = None
) -> dict | None:
    path = deployment_plan_path(scope, repo_root=repo_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
