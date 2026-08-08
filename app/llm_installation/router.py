from __future__ import annotations

import os
from typing import Any

from document_ai.services.rag_runtime_config import (
    LLMRuntimeNotConfigured,
    ServerRAGTarget,
)
from llm_installation.catalog import (
    RAGModelCatalogEntry,
    evaluate_catalog_fit,
    get_rag_model_catalog,
)
from llm_installation.planner import (
    CatalogAssessment,
    assess_catalog_entry,
    build_serving_plan,
)
from llm_installation.selection import (
    SelectionCandidate,
    select_catalog_model,
)
from llm_installation.runtime_handoff import (
    build_runtime_policy_input,
)
from llm_installation.runtime_probe import (
    EndpointStatus,
    ServerRuntimeProfile,
    SmokeTestStatus,
    check_endpoint_health,
    check_openai_compatible_endpoint,
    probe_server_runtime,
    smoke_test_chat_completion,
)


def _runtime_rejection_reason(entry: RAGModelCatalogEntry, profile: ServerRuntimeProfile) -> str:
    fit = evaluate_catalog_fit(entry, profile)
    if fit.fit_status == "NOFIT":
        return fit.summary
    return ""


def _fits_runtime(entry: RAGModelCatalogEntry, profile: ServerRuntimeProfile) -> bool:
    return not _runtime_rejection_reason(entry, profile)


def _status_to_dict(status) -> dict[str, Any] | None:
    if status is None:
        return None
    return {
        key: value
        for key, value in vars(status).items()
        if not key.startswith("_")
    }


def _candidate_diagnostic(
    entry: RAGModelCatalogEntry,
    *,
    selected: bool = False,
    rejected_reason: str = "",
    health_status: EndpointStatus | None = None,
    endpoint_status: EndpointStatus | None = None,
    smoke_status: SmokeTestStatus | None = None,
    priority_preset: str = "balanced",
    serving_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = serving_profile.get("runtime") if serving_profile else "unknown"
    base_url = serving_profile.get("base_url") if serving_profile else ""
    return {
        "model": entry.id,
        "runtime": runtime,
        "base_url": base_url,
        "priority": entry.priority,
        "selected": selected,
        "rejected_reason": rejected_reason,
        "health_status": _status_to_dict(health_status),
        "endpoint_status": _status_to_dict(endpoint_status),
        "smoke_status": _status_to_dict(smoke_status),
        "priority_preset": priority_preset,
        "serving_profile": serving_profile or {},
    }


def _diagnostics(profile: ServerRuntimeProfile, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "profile": {
            "cpu_count": profile.cpu_count,
            "ram_mb": profile.ram_mb,
            "disk_free_mb": profile.disk_free_mb,
            "container_memory_limit_mb": profile.container_memory_limit_mb,
            "has_gpu": profile.has_gpu,
            "gpu_count": profile.gpu_count,
            "gpu_name": profile.gpu_name,
            "gpu_vram_mb": profile.gpu_vram_mb,
            "gpu_vram_list": profile.gpu_vram_list or [],
            "cuda_driver_version": profile.cuda_driver_version,
            "driver_supported_cuda_version": profile.driver_supported_cuda_version,
            "docker_available": profile.docker_available,
            "docker_compose_available": profile.docker_compose_available,
            "docker_services": profile.docker_services or [],
            "disk_total_mb": profile.disk_total_mb,
            "ram_available_mb": profile.ram_available_mb,
            "gpu_vram_free_mb": profile.gpu_vram_free_mb,
            "gpu_vram_free_list": profile.gpu_vram_free_list or [],
            "gpu_compute_cap": profile.gpu_compute_cap,
            "gpu_mem_bandwidth_gb_s": profile.gpu_mem_bandwidth_gb_s,
            "cluster_mode": profile.cluster_mode,
            "cpu_model": profile.cpu_model,
            "cpu_features": profile.cpu_features or [],
        },
        "candidates": candidates,
    }


def resolve_server_rag_target(
    *,
    profile: ServerRuntimeProfile | None = None,
    catalog: list[RAGModelCatalogEntry] | None = None,
    check_endpoint: bool | None = None,
    smoke_test: bool = False,
    selection_mode: str = "automatic",
    selected_artifact_id: str | None = None,
    risky_confirmed: bool = False,
    priority_preset: str = "balanced",
) -> ServerRAGTarget:
    profile = profile or probe_server_runtime()
    catalog = catalog if catalog is not None else get_rag_model_catalog()
    should_check_endpoint = (
        os.getenv("RAG_AUTO_CHECK_ENDPOINT", "0").strip().lower() in {"1", "true", "yes", "on"}
        if check_endpoint is None
        else check_endpoint
    )

    candidate_diagnostics: list[dict[str, Any]] = []
    selection_candidates: list[SelectionCandidate] = []
    for entry in catalog:
        assessment = assess_catalog_entry(entry, profile)
        fit_evaluation = evaluate_catalog_fit(entry, profile)
        rejection_reason = assessment.fit_reason if assessment.fit_status == "NOFIT" else ""
        if rejection_reason:
            candidate_diagnostics.append(
                _candidate_diagnostic(
                    entry,
                    rejected_reason=rejection_reason,
                    priority_preset=priority_preset,
                    serving_profile=assessment.as_dict(),
                )
            )
        selection_candidates.append(
            SelectionCandidate(entry, assessment, fit_evaluation)
        )

    selection_result = select_catalog_model(
        selection_candidates,
        priority_preset,
        selection_mode=selection_mode,
        selected_artifact_id=selected_artifact_id,
        risky_confirmed=risky_confirmed,
    )
    if selection_result.selection_status == "MANUAL_SELECTION_REQUIRED":
        raise LLMRuntimeNotConfigured("Manual selection requires an artifact ID.")
    if selection_result.selection_status == "INVALID_MANUAL_SELECTION":
        raise LLMRuntimeNotConfigured("The selected artifact is not manually selectable.")
    if selection_result.selection_status == "RISKY_CONFIRMATION_REQUIRED":
        raise LLMRuntimeNotConfigured(
            "The selected RISKY model requires explicit confirmation."
        )
    if selection_result.selection_status != "SELECTED":
        raise LLMRuntimeNotConfigured(
            "No selectable FIT or confirmed RISKY catalog assessment exists."
        )

    # Stage 5 selects exactly one model before endpoint/runtime validation.
    selected_candidate = selection_result.selected_candidate
    runtime_policy_input = build_runtime_policy_input(
        selection_result,
        profile,
    )
    candidates = [(selected_candidate.entry, selected_candidate.assessment)]

    failed_statuses: list[str] = []
    for entry, assessment in candidates:
        plan = build_serving_plan(runtime_policy_input)
        serving_profile = plan.as_dict()
        health_status = None
        endpoint_status = None
        smoke_status = None
        if should_check_endpoint:
            health_status = check_endpoint_health(plan.base_url)
            endpoint_status = check_openai_compatible_endpoint(
                plan.base_url,
                expected_model=entry.id,
            )
            if not endpoint_status.ok:
                failed_statuses.append(f"{plan.runtime}:{endpoint_status.message}")
                candidate_diagnostics.append(
                    _candidate_diagnostic(
                        entry,
                        rejected_reason=endpoint_status.message,
                        health_status=health_status,
                        endpoint_status=endpoint_status,
                        priority_preset=priority_preset,
                        serving_profile=serving_profile,
                    )
                )
                continue
        if smoke_test:
            smoke_status = smoke_test_chat_completion(plan.base_url, entry.id)
            if not smoke_status.ok:
                failed_statuses.append(f"{plan.runtime}:{smoke_status.message}")
                candidate_diagnostics.append(
                    _candidate_diagnostic(
                        entry,
                        rejected_reason=smoke_status.message,
                        health_status=health_status,
                        endpoint_status=endpoint_status,
                        smoke_status=smoke_status,
                        priority_preset=priority_preset,
                        serving_profile=serving_profile,
                    )
                )
                continue
        candidate_diagnostics.append(
            _candidate_diagnostic(
                entry,
                selected=True,
                health_status=health_status,
                endpoint_status=endpoint_status,
                smoke_status=smoke_status,
                priority_preset=priority_preset,
                serving_profile=serving_profile,
            )
        )
        return ServerRAGTarget(
            endpoint_name="Server auto",
            base_url=plan.base_url,
            model=entry.id,
            runtime=plan.runtime,
            reason=(
                f"Selected {plan.runtime} from {entry.source}; "
                f"cpu={profile.cpu_count}, ram_mb={profile.ram_mb}, "
                f"gpu_vram_mb={profile.gpu_vram_mb}."
            ),
            fallback_used=False,
            endpoint_status=endpoint_status,
            health_status=health_status,
            smoke_status=smoke_status,
            diagnostics=_diagnostics(profile, candidate_diagnostics),
            priority_preset=priority_preset,
            selection_mode=selection_mode,
            selection_reason_code=selection_result.reason_code,
            runtime_policy_input=runtime_policy_input.as_dict(),
            serving_profile=serving_profile,
        )

    raise LLMRuntimeNotConfigured(
        "Selected runtime failed validation: " + "; ".join(failed_statuses)
    )
