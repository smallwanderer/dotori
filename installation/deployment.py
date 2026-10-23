from __future__ import annotations

from capabilities.functions import candidate_identity, get_function, validate_function_contract

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from capabilities.config import LEGACY_DOCUMENT_CAPABILITIES, build_capability_config
from capabilities.execution import EXECUTION_ROLES
from capabilities.modules import DOCUMENTS_MODULE, resolve_modules
from capabilities.work_registry import WORK_REGISTRY
from capabilities.registry import DOCUMENT_EXTRACT, get_capability

if TYPE_CHECKING:
    from llm_installation.runtime_lifecycle import RuntimeSpec


SCOPES = ("production",)
MODES = ("rag", "search", "extract", "basic")
NETWORK_ACCESS_MODES = ("local", "direct_https")
CORE_SERVICES = ("db", "redis", "app", "dotori-orchestrator")
DOCUMENT_MODEL_SERVICE = "embedding-executor"
DOCUMENT_QUEUE_SERVICES = ("dotori-orchestrator",)
DOCUMENT_EXECUTOR_SERVICES = ("parser-executor", "embedding-executor")
# Kept under the existing public name because install.py and saved deployment
# tooling already import it. It represents every non-core Compose service that
# must follow the document-processing lifecycle, including the model endpoint.
ALL_WORKER_SERVICES = DOCUMENT_EXECUTOR_SERVICES
DOCUMENT_RUNTIME_RESTART_SERVICES = ("app", *DOCUMENT_QUEUE_SERVICES, *ALL_WORKER_SERVICES)


def worker_services_for_lifecycle(saved_plan: dict | None = None) -> tuple[str, ...]:
    """Return every current service represented by a persisted plan.

    Deployment plans are persisted across upgrades. A plan written before the
    parse/embed split only contains the legacy dotori-document service, so lifecycle operations
    must not treat that old snapshot as the complete current topology. Removed
    service names are ignored because Compose cannot stop an undefined service.
    """
    planned_services: list[str] = []
    if saved_plan:
        planned_services = [
            worker.get("compose_service")
            for worker in saved_plan.get("workers", [])
            if worker.get("enabled")
            and worker.get("compose_service") in ALL_WORKER_SERVICES
        ]
    return tuple(dict.fromkeys((*planned_services, *DOCUMENT_QUEUE_SERVICES, *ALL_WORKER_SERVICES)))


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


def embedding_runtime_change_command(
    compose_command: str,
    *,
    scope: str,
    priority_preset: str = "balanced",
    catalog_id: str | None = None,
    candidate_generation_id: str | None = None,
    force_reembed: bool = False,
    skip_reembed: bool = False,
    activate: bool = True,
) -> str:
    """Build the one-off model-owner command for a candidate generation.

    The regular app image deliberately excludes local model dependencies, and
    the running model endpoint is pinned to the active runtime. The candidate
    must therefore be built in the AI image with in-process model ownership.
    The service's normal config mount is read-only, so this maintenance command
    explicitly replaces it with the writable host config mount.
    """
    args = [f"--scope {scope}"]
    if candidate_generation_id:
        args.append(f"--candidate-generation-id {candidate_generation_id}")
    elif catalog_id:
        args.append(f"--catalog-id {catalog_id}")
    else:
        args.append(f"--preset {priority_preset}")

    if activate:
        args.append("--activate")
    if force_reembed:
        args.append("--force-reembed")
    if skip_reembed:
        args.append("--skip-reembed")

    args_str = " ".join(args)
    return (
        f"{compose_command} run --rm "
        "-e DOTORI_EMBEDDING_MODEL_PROCESS=1 "
        "-v ./data/config:/data/config "
        "-v ./app:/app "
        f"{DOCUMENT_MODEL_SERVICE} python manage.py change_embedding_runtime "
        f"{args_str}"
    )


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
        if not self.queues and self.health_strategy == "celery-ping":
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
    capability_revision: str
    enabled_capabilities: tuple[str, ...]

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
            "capability_revision": self.capability_revision,
            "enabled_capabilities": list(self.enabled_capabilities),
            "execution_roles": {
                role: {
                    "transport": spec.transport,
                    "config_source": spec.config_source,
                    "compose_service": spec.compose_service,
                }
                for role, spec in EXECUTION_ROLES.items()
                if (
                    (spec.compose_service and spec.compose_service in self.enabled_services)
                    or (role == "llm" and self.runtime is not None)
                )
            },
            "work_routes": {
                kind: definition.deployment_entry()
                for kind, definition in WORK_REGISTRY.items()
                if definition.capability_id in self.enabled_capabilities
            },
        }


def normalize_mode(mode: str) -> str:
    aliases = {
        "1": "rag",
        "2": "search",
        "3": "basic",
        "4": "extract",
        "documents": "extract",
        "full": "rag",
        "full-rag": "rag",
    }
    normalized = aliases.get(str(mode).strip().lower(), str(mode).strip().lower())
    if normalized not in MODES:
        raise ValueError(f"Unknown deployment mode: {mode}")
    return normalized


def _worker_specs(mode: str, *, runtime_available: bool) -> tuple[WorkerSpec, ...]:
    parser_enabled = mode in {"rag", "search", "extract"}
    embedding_enabled = mode in {"rag", "search"}
    return (
        WorkerSpec(
            name="parser-executor",
            compose_service="parser-executor",
            queues=(),
            concurrency=1,
            prefetch_multiplier=1,
            enabled=parser_enabled,
            requires_runtime=False,
            dependencies=("db", "redis"),
            health_strategy="http-readyz",
        ),
        WorkerSpec(
            name="embedding-executor",
            compose_service=DOCUMENT_MODEL_SERVICE,
            queues=(),
            concurrency=1,
            prefetch_multiplier=1,
            enabled=embedding_enabled,
            requires_runtime=False,
            dependencies=("db", "redis"),
            health_strategy="http-readyz",
        ),
    )


def _generation_id(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"plan-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"


def reusable_verified_llm_projection(
    previous_plan: dict | None,
    llm_runtime_config: dict | None,
    *,
    mode: str,
    llm_enabled: bool = True,
) -> dict:
    """Carry a verified LLM declaration across a plan rebuild for the same runtime.

    Runtime generations are immutable. When an older active runtime snapshot lacks
    newer catalog metadata, an already-attested deployment plan remains the source
    of that metadata until an explicit replan or a runtime generation change.
    """
    if not llm_enabled or normalize_mode(mode) != "rag":
        return {}
    if not isinstance(previous_plan, dict) or previous_plan.get("schema_version", 0) < 3:
        return {}
    if not isinstance(llm_runtime_config, dict):
        return {}
    target = llm_runtime_config.get("target")
    if not isinstance(target, dict):
        return {}
    generation_id = str(target.get("generation_id") or "").strip()
    model_id = str(target.get("model") or "").strip()
    if not generation_id or not model_id:
        return {}
    if str(previous_plan.get("runtime_generation") or "") != generation_id:
        return {}

    policy_input = target.get("runtime_policy_input")
    assessment = (
        policy_input.get("catalog_assessment")
        if isinstance(policy_input, dict)
        else None
    )
    assessment = assessment if isinstance(assessment, dict) else {}
    serving_profile = target.get("serving_profile")
    serving_profile = serving_profile if isinstance(serving_profile, dict) else {}
    backend_id = str(
        assessment.get("backend_profile")
        or serving_profile.get("backend_profile")
        or ""
    ).strip()
    installation_id = f"llm:{generation_id}"

    prior_models = previous_plan.get("installed_models")
    prior_model = prior_models.get(installation_id) if isinstance(prior_models, dict) else None
    if not isinstance(prior_model, dict):
        return {}
    if (
        prior_model.get("role") != "llm"
        or prior_model.get("catalog_id") != model_id
        or not prior_model.get("artifact_revision")
        or not prior_model.get("artifact_digest")
        or (backend_id and prior_model.get("backend_id") != backend_id)
    ):
        return {}

    current_entry = assessment.get("catalog_entry")
    current_function_ids = {
        item.get("function_id")
        for item in current_entry.get("functions", [])
        if isinstance(item, dict) and isinstance(item.get("function_id"), str)
    } if isinstance(current_entry, dict) else set()
    prior_functions = previous_plan.get("function_providers")
    if not isinstance(prior_functions, dict):
        return {}

    carried_functions: dict[str, list[dict]] = {}
    for function_id, candidates in prior_functions.items():
        if function_id in current_function_ids or not isinstance(candidates, list):
            continue
        matching = [
            dict(candidate)
            for candidate in candidates
            if isinstance(candidate, dict)
            and candidate.get("model_installation_id") == installation_id
            and candidate.get("role") == "llm"
            and candidate.get("verification") in {"contract_verified", "quality_verified"}
            and candidate.get("evidence_ref")
            and (not backend_id or candidate.get("backend_id") == backend_id)
        ]
        if matching:
            carried_functions[function_id] = matching
    if not carried_functions:
        return {}

    prior_routes = previous_plan.get("default_routes")
    carried_routes = {
        function_id: installation_id
        for function_id in carried_functions
        if isinstance(prior_routes, dict)
        and prior_routes.get(function_id) == installation_id
    }
    return {
        "installed_models": {installation_id: dict(prior_model)},
        "function_providers": carried_functions,
        "default_routes": carried_routes,
    }


def build_deployment_plan(
    mode: str,
    *,
    scope: str = "production",
    runtime: RuntimeSpec | None = None,
    network_access: str = "local",
) -> DeploymentPlan:
    normalized_mode = normalize_mode(mode)
    if normalized_mode in {"rag", "search"}:
        enabled_capabilities = LEGACY_DOCUMENT_CAPABILITIES
    elif normalized_mode == "extract":
        enabled_capabilities = (DOCUMENT_EXTRACT,)
    else:
        enabled_capabilities = ()
    capabilities = build_capability_config(enabled_capabilities)
    workers = _worker_specs(normalized_mode, runtime_available=runtime is not None)
    unsigned = {
        "scope": scope,
        "mode": normalized_mode,
        "core_services": CORE_SERVICES,
        "workers": [worker.as_dict() for worker in workers],
        "runtime_generation": runtime.generation_id if runtime else None,
        "network_access": network_access,
        "capability_revision": capabilities.revision,
        "enabled_capabilities": capabilities.enabled_capabilities,
    }
    return DeploymentPlan(
        scope=scope,
        mode=normalized_mode,
        core_services=CORE_SERVICES,
        workers=workers,
        runtime=runtime,
        network_access=network_access,
        generation_id=_generation_id(unsigned),
        capability_revision=capabilities.revision,
        enabled_capabilities=capabilities.enabled_capabilities,
    )


def build_module_deployment_plan(
    selected_modules: list[str] | tuple[str, ...],
    *,
    scope: str = "production",
    runtime: RuntimeSpec | None = None,
    network_access: str = "local",
    disabled_provider_roles: tuple[str, ...] = (),
    installed_models: dict[str, dict] | None = None,
    function_providers: dict[str, list[dict]] | None = None,
    default_routes: dict[str, str] | None = None,
    embedding_runtime_config: dict | None = None,
    llm_runtime_config: dict | None = None,
    execution_observations: dict | None = None,
) -> dict:
    """Build a side-effect-free deployment declaration from modules."""
    if scope not in SCOPES:
        raise ValueError(f"Unknown deployment scope: {scope}")
    if network_access not in NETWORK_ACCESS_MODES:
        raise ValueError(f"Unknown network access mode: {network_access}")
    if runtime is not None and runtime.scope != scope:
        raise ValueError("RuntimeSpec scope must match deployment scope.")

    modules = resolve_modules(selected_modules)
    installed_capabilities = tuple(
        dict.fromkeys(
            capability for module in modules for capability in module.capabilities
        )
    )
    disabled = set(disabled_provider_roles)
    provider_requirements = {
        provider.role: provider
        for module in modules
        for provider in module.providers
    }
    unknown_roles = sorted(disabled - set(provider_requirements))
    if unknown_roles:
        raise ValueError(f"Unknown provider roles: {', '.join(unknown_roles)}")

    provider_bindings = {
        role: {
            "provider_id": provider.provider_id,
            "config_source": provider.config_source,
            "enabled": provider.default_enabled and role not in disabled,
        }
        for role, provider in provider_requirements.items()
    }
    installed_model_entries = dict(installed_models or {})
    function_provider_entries: dict[str, list[dict]] = {}
    llm_target = llm_runtime_config.get("target") if isinstance(llm_runtime_config, dict) else None
    llm_model_id = llm_target.get("model") if isinstance(llm_target, dict) else None
    llm_generation_id = llm_target.get("generation_id") if isinstance(llm_target, dict) else None
    llm_policy_input = (
        llm_target.get("runtime_policy_input")
        if isinstance(llm_target, dict)
        else None
    )
    llm_assessment = (
        llm_policy_input.get("catalog_assessment")
        if isinstance(llm_policy_input, dict)
        else None
    )
    llm_catalog_entry = (
        llm_assessment.get("catalog_entry")
        if isinstance(llm_assessment, dict)
        else None
    )
    if not isinstance(llm_catalog_entry, dict) or llm_catalog_entry.get("id") != llm_model_id:
        llm_catalog_entry = None
    llm_backend_id = (
        llm_assessment.get("backend_profile")
        if isinstance(llm_assessment, dict)
        else None
    )
    if not llm_backend_id and isinstance(llm_target, dict):
        serving_profile = llm_target.get("serving_profile")
        if isinstance(serving_profile, dict):
            llm_backend_id = serving_profile.get("backend_profile")
    if runtime is not None:
        llm_model_id = llm_model_id or runtime.model_id
        llm_generation_id = llm_generation_id or runtime.generation_id
    if llm_model_id and llm_generation_id:
        installation_id = f"llm:{llm_generation_id}"
        hf = llm_catalog_entry.get("hf") if llm_catalog_entry else None
        artifact = llm_catalog_entry.get("artifact") if llm_catalog_entry else None
        model_entry = {
            "role": "llm",
            "catalog_id": llm_model_id,
            "artifact_revision": (
                hf.get("revision")
                if isinstance(hf, dict) and hf.get("revision")
                else llm_generation_id
            ),
            "runtime_ref": "llm_runtime.json",
        }
        if isinstance(artifact, dict) and artifact.get("sha256"):
            model_entry["artifact_digest"] = artifact["sha256"]
        if llm_backend_id:
            model_entry["backend_id"] = llm_backend_id
        installed_model_entries.setdefault(
            installation_id,
            model_entry,
        )
    if isinstance(embedding_runtime_config, dict):
        generation_id = str(embedding_runtime_config.get("generation_id") or "").strip()
        catalog_id = str(embedding_runtime_config.get("catalog_id") or "").strip()
        if generation_id and catalog_id:
            installation_id = f"embedding:{generation_id}"
            installed_model_entries.setdefault(
                installation_id,
                {
                    "role": "embedding",
                    "catalog_id": catalog_id,
                    "artifact_revision": str(
                        embedding_runtime_config.get("model_revision")
                        or embedding_runtime_config.get("runtime_fingerprint")
                        or generation_id
                    ),
                    "runtime_ref": "embedding_runtime.json",
                },
            )
    for role, runtime_payload in (
        ("embedding", embedding_runtime_config),
        ("llm", llm_runtime_config),
    ):
        if not isinstance(runtime_payload, dict):
            continue
        installation_id = next(
            (
                model_id for model_id, model in installed_model_entries.items()
                if model.get("role") == role
            ),
            None,
        )
        if not installation_id:
            continue
        catalog_id = str(
            llm_model_id
            if role == "llm"
            else runtime_payload.get("catalog_id") or ""
        ).strip()
        backend_id = (
            llm_backend_id
            if role == "llm"
            else str(runtime_payload.get("provider") or "").strip()
        )
        functions = runtime_payload.get("functions", [])
        if role == "llm":
            functions = (
                llm_catalog_entry.get("functions", [])
                if llm_catalog_entry
                else []
            )
        if not isinstance(functions, list):
            continue
        for function in functions:
            if not isinstance(function, dict) or not isinstance(function.get("function_id"), str):
                continue
            artifact_ids = function.get("artifact_ids") or []
            backend_ids = function.get("backend_ids") or []
            if artifact_ids and catalog_id not in artifact_ids:
                continue
            if backend_ids and backend_id not in backend_ids:
                continue
            candidate = {
                "model_installation_id": installation_id,
                "role": role,
                "preferred_role": function.get("preferred_role"),
                "verification": function.get("verification"),
                "evidence_ref": function.get("evidence_ref"),
                "declaration_ref": function.get("declaration_ref"),
                "adapter_contract": function.get("adapter_contract"),
            }
            if backend_id:
                candidate["backend_id"] = backend_id
            function_provider_entries.setdefault(function["function_id"], []).append(
                candidate
            )
    for function_id, candidates in (function_providers or {}).items():
        function_provider_entries.setdefault(function_id, []).extend(dict(candidate) for candidate in candidates)
    installed_provider_entries = {}
    if "parser" in provider_bindings:
        provider_id = "parser:docling:1"
        installed_provider_entries[provider_id] = {
            "role": "parser", "provider_id": "docling", "registration_revision": "1",
            "adapter_contract": "dotori-parser-http-v1", "config_source": "DOCUMENT_PARSER_BACKEND",
        }
        provider_bindings["parser"]["provider_installation_id"] = provider_id
        function_provider_entries.setdefault("document.extract", []).append({
            "provider_installation_id": provider_id, "role": "parser",
            "verification": "declared_unverified", "declaration_ref": "dotori-parser-http-v1",
            "adapter_contract": "dotori-parser-http-v1",
        })
    for function_id, candidates in function_provider_entries.items():
        definition = get_function(function_id)
        for candidate in candidates:
            for key, value in definition.metadata().items():
                if key == "preferred_role" and candidate.get(key) is None:
                    candidate[key] = value
                else:
                    candidate.setdefault(key, value)
            validate_function_contract(function_id, candidate)
    from capabilities.function_scope import project_request_scope
    from llm_installation.function_catalog import load_function_evidence
    for candidate in function_provider_entries.get("text.generate", []):
        if candidate.get("verification") in {"contract_verified", "quality_verified"}:
            record = load_function_evidence(candidate["evidence_ref"])
            if record.function_id != "text.generate":
                raise ValueError("Request scope evidence function mismatch")
            candidate["request_evidence_scope"] = project_request_scope(record)
    default_route_entries = dict(default_routes or {})
    # Explicit observations activate applicability enforcement for this replan.
    # None preserves existing callers until all installation producers are wired;
    # an empty mapping explicitly means no executing backend was observed.
    if execution_observations is not None:
        if not isinstance(execution_observations, dict) or not all(isinstance(value, dict) for value in execution_observations.values()):
            raise ValueError("execution_observations must map installation IDs to function observations")
        from llm_installation.function_applicability import ObservedFunctionExecution, assess_evidence_applicability
        from llm_installation.function_catalog import load_function_evidence
        for function_id, candidates in function_provider_entries.items():
            for candidate in candidates:
                if candidate.get("verification") not in {"contract_verified", "quality_verified"}:
                    continue
                identity = candidate_identity(candidate)
                raw = execution_observations.get(identity, {}).get(function_id)
                observed = ObservedFunctionExecution.model_validate(raw) if raw is not None else None
                generation = identity.partition(":")[2]
                assessment = assess_evidence_applicability(
                    load_function_evidence(candidate["evidence_ref"]), observed,
                    expected_runtime_generation_id=generation)
                if observed is not None:
                    model = installed_model_entries.get(identity, {})
                    active = embedding_runtime_config if candidate.get("role") == "embedding" else None
                    if (model.get("catalog_id") != observed.target_id
                            or model.get("artifact_revision") != observed.target_revision
                            or model.get("artifact_digest", model.get("artifact_revision")) != observed.artifact_digest):
                        assessment["reasons"].append("active_artifact_mismatch")
                    if candidate.get("role") == "embedding":
                        if not isinstance(active, dict):
                            assessment["reasons"].append("active_settings_missing")
                        elif (active.get("generation_id") != observed.runtime_generation_id
                              or active.get("provider") != observed.backend_id
                              or any(active.get(key) != observed.inference_options.get(key) for key in (
                                  "dimension", "distance_strategy", "document_prefix", "max_tokens",
                                  "normalize_embeddings", "query_prefix", "store"))):
                            assessment["reasons"].append("active_settings_mismatch")
                    else:
                        # Server generation defaults are not the actual Workspace request options.
                        assessment["reasons"].append("active_request_settings_unobserved")
                    if assessment["reasons"]:
                        assessment["status"] = "revalidation_required"
                        assessment.pop("verification", None)
                candidate["evidence_applicability"] = assessment
                if assessment["status"] != "applicable":
                    import hashlib
                    candidate["prior_evidence_ref"] = candidate.pop("evidence_ref")
                    candidate["verification"] = "declared_unverified"
                    candidate["declaration_ref"] = "revalidation-" + hashlib.sha256(
                        candidate["prior_evidence_ref"].encode()).hexdigest()[:16]
                    if default_route_entries.get(function_id) == identity:
                        default_route_entries.pop(function_id)

    if not all(isinstance(key, str) and isinstance(value, dict) for key, value in installed_model_entries.items()):
        raise ValueError("installed_models must map installation IDs to objects")
    if len(installed_model_entries) != len(set(installed_model_entries)):
        raise ValueError("installed_models must not contain duplicate IDs")
    for model_id, model in installed_model_entries.items():
        if not isinstance(model.get("role"), str) or not model.get("role"):
            raise ValueError(f"Installed model {model_id} requires a role")
        for field in ("catalog_id", "artifact_revision", "runtime_ref"):
            if not isinstance(model.get(field), str) or not model.get(field):
                raise ValueError(f"Installed model {model_id} requires {field}")
    for role, binding in provider_bindings.items():
        candidates = [
            model_id
            for model_id, model in installed_model_entries.items()
            if model.get("role") == role
        ]
        if role == "embedding" and len(candidates) > 1:
            raise ValueError("The server may install only one embedding model")
        selected_model = binding.get("model_installation_id")
        if selected_model is None and len(candidates) == 1:
            binding["model_installation_id"] = candidates[0]
        elif selected_model is not None:
            model = installed_model_entries.get(selected_model)
            if not isinstance(model, dict) or model.get("role") != role:
                raise ValueError(f"Role {role} references an incompatible installed model")
    if not all(
        isinstance(function_id, str)
        and isinstance(candidates, list)
        and all(isinstance(candidate, dict) for candidate in candidates)
        for function_id, candidates in function_provider_entries.items()
    ):
        raise ValueError("function_providers must map function IDs to lists of objects")
    for function_id, candidates in function_provider_entries.items():
        seen_models = set()
        for candidate in candidates:
            model_id = candidate_identity(candidate)
            references = installed_provider_entries if candidate.get("provider_installation_id") else installed_model_entries
            grade = candidate.get("verification")
            if model_id not in references:
                raise ValueError(f"Function {function_id} references an unknown installed model")
            if candidate.get("role") != references[model_id].get("role"):
                raise ValueError(f"Function {function_id} provider role does not match its model")
            if model_id in seen_models:
                raise ValueError(f"Function {function_id} contains a duplicate model candidate")
            seen_models.add(model_id)
            if grade not in {"declared_unverified", "contract_verified", "quality_verified"}:
                raise ValueError(f"Function {function_id} has an invalid verification grade")
            if grade == "declared_unverified" and not candidate.get("declaration_ref"):
                raise ValueError(f"Function {function_id} requires a declaration_ref")
            if grade in {"contract_verified", "quality_verified"} and not candidate.get("evidence_ref"):
                raise ValueError(f"Function {function_id} requires an evidence_ref")
    if not all(
        isinstance(function_id, str) and isinstance(model_id, str)
        for function_id, model_id in default_route_entries.items()
    ):
        raise ValueError("default_routes must map function IDs to model installation IDs")
    for role, binding in provider_bindings.items():
        selected_model = binding.get("model_installation_id")
        if selected_model is not None and selected_model not in installed_model_entries:
            raise ValueError(f"Role {role} references unknown installed model {selected_model}")
    embedding_model_ids = {
        candidate.get("model_installation_id")
        for candidates in function_provider_entries.values()
        for candidate in candidates
        if candidate.get("role") == "embedding"
    }
    if len(embedding_model_ids) > 1:
        raise ValueError("The server may register only one embedding model")
    for function_id, model_id in default_route_entries.items():
        candidates = function_provider_entries.get(function_id, [])
        if not any(
            (candidate.get("model_installation_id") or candidate.get("provider_installation_id")) == model_id
            and (
                candidate.get("verification") == "quality_verified"
                or (
                    function_id in {"text.generate", "text.embed"}
                    and candidate.get("verification") == "contract_verified"
                )
            )
            and provider_bindings.get(candidate.get("role"), {}).get("enabled")
            for candidate in candidates
        ):
            raise ValueError(f"Default route {function_id} must select a verified provider")
    for function_id, candidates in function_provider_entries.items():
        if function_id in default_route_entries:
            continue
        verified = [
            candidate for candidate in candidates
            if provider_bindings.get(candidate.get("role"), {}).get("enabled")
            and (
                candidate.get("verification") == "quality_verified"
                or (
                    function_id in {"text.generate", "text.embed"}
                    and candidate.get("verification") == "contract_verified"
                )
            )
        ]
        preferred_roles = {
            candidate.get("preferred_role")
            for candidate in verified
            if candidate.get("preferred_role")
        }
        if len(preferred_roles) == 1:
            preferred_role = next(iter(preferred_roles))
            preferred = [candidate for candidate in verified if candidate.get("role") == preferred_role]
            if preferred:
                verified = preferred
        if len(verified) == 1:
            default_route_entries[function_id] = candidate_identity(verified[0])
    enabled_roles = {
        role for role, binding in provider_bindings.items() if binding["enabled"]
    }
    enabled_capabilities = tuple(
        capability
        for capability in installed_capabilities
        if set(get_capability(capability).execution_roles).issubset(enabled_roles)
    )
    capability_revision = build_capability_config(enabled_capabilities).revision
    optional_services = tuple(
        dict.fromkeys(
            spec.compose_service
            for role, spec in EXECUTION_ROLES.items()
            if role in enabled_roles and spec.compose_service
        )
    )
    execution_roles = {
        role: {
            "transport": EXECUTION_ROLES[role].transport,
            "config_source": EXECUTION_ROLES[role].config_source,
            "compose_service": EXECUTION_ROLES[role].compose_service,
        }
        for role in provider_requirements
        if role in enabled_roles
    }
    selected_work_kinds = {
        kind for module in modules for kind in module.work_kinds
    }
    work_routes = {
        kind: definition.deployment_entry()
        for kind, definition in WORK_REGISTRY.items()
        if kind in selected_work_kinds
        and definition.capability_id in enabled_capabilities
        and definition.execution_role in enabled_roles
    }
    queues = tuple(dict.fromkeys(route["queue"] for route in work_routes.values()))
    unsigned = {
        "scope": scope,
        "selected_modules": [module.module_id for module in modules],
        "installed_capabilities": list(installed_capabilities),
        "enabled_capabilities": list(enabled_capabilities),
        "role_bindings": provider_bindings,
        # Read-compatible alias for callers that still inspect the schema-2 key.
        "provider_bindings": provider_bindings,
        "installed_models": installed_model_entries,
        "installed_providers": installed_provider_entries,
        "function_providers": function_provider_entries,
        "default_routes": default_route_entries,
        "execution_roles": execution_roles,
        "services": [*CORE_SERVICES, *optional_services],
        "queues": list(queues),
        "runtime_generation": runtime.generation_id if runtime else None,
        "network_access": network_access,
        "capability_revision": capability_revision,
        "work_routes": work_routes,
    }
    runtime_payload = asdict(runtime) if runtime is not None else None
    if runtime_payload is not None:
        runtime_payload["args_file"] = str(runtime_payload["args_file"])
    return {
        **unsigned,
        "schema_version": 4,
        "generation_id": _generation_id(unsigned),
        "runtime": runtime_payload,
    }


def legacy_module_selection(mode: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Translate the retired mode input at the installation compatibility edge."""
    normalized = normalize_mode(mode)
    if normalized == "basic":
        return (), ()
    disabled_roles = {
        "extract": ("embedding", "llm"),
        "search": ("llm",),
        "rag": (),
    }[normalized]
    return (DOCUMENTS_MODULE,), disabled_roles


def build_legacy_module_deployment_plan(
    mode: str,
    *,
    scope: str = "production",
    runtime: RuntimeSpec | None = None,
    network_access: str = "local",
    disabled_provider_roles: tuple[str, ...] = (),
    installed_models: dict[str, dict] | None = None,
    function_providers: dict[str, list[dict]] | None = None,
    default_routes: dict[str, str] | None = None,
    embedding_runtime_config: dict | None = None,
    llm_runtime_config: dict | None = None,
    execution_observations: dict | None = None,
) -> dict:
    """Compatibility adapter; new callers pass module IDs directly."""
    selected_modules, compatibility_disabled_roles = legacy_module_selection(mode)
    disabled_roles = tuple(
        dict.fromkeys((*compatibility_disabled_roles, *disabled_provider_roles))
    )
    return build_module_deployment_plan(
        selected_modules,
        scope=scope,
        runtime=runtime,
        network_access=network_access,
        disabled_provider_roles=disabled_roles,
        installed_models=installed_models,
        function_providers=function_providers,
        default_routes=default_routes,
        embedding_runtime_config=embedding_runtime_config,
        llm_runtime_config=llm_runtime_config,
        execution_observations=execution_observations,
    )


def deployment_plan_path(
    scope: str, *, repo_root: Path | None = None
) -> Path:
    if scope not in SCOPES:
        raise ValueError(f"Unknown deployment scope: {scope}")
    root = repo_root or Path(__file__).resolve().parents[1]
    return root / "data" / "config" / "deployment_scopes" / scope / "deployment_plan.json"


def write_deployment_plan(
    plan: DeploymentPlan | dict, *, repo_root: Path | None = None
) -> Path:
    scope = plan.scope if isinstance(plan, DeploymentPlan) else plan.get("scope")
    if not isinstance(scope, str):
        raise ValueError("Deployment plan scope is required.")
    path = deployment_plan_path(scope, repo_root=repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            plan.as_dict() if isinstance(plan, DeploymentPlan) else plan,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
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
