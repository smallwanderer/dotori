from document_ai.llm_installation_helper.catalog.evaluator import (
    FitEvaluation,
    evaluate_catalog_fit as _evaluate_catalog_fit,
)
from document_ai.llm_installation_helper.catalog.loader import (
    get_catalog_entry,
    get_rag_model_catalog,
    search_rag_model_catalog,
)
from document_ai.llm_installation_helper.catalog.models import RAGModelCatalogEntry
from document_ai.llm_installation_helper.planner import assess_catalog_entry


def _mb_label(value: int) -> str:
    if not value:
        return "-"
    if value >= 1024:
        gb = value / 1024
        return f"{gb:.0f}GB" if gb.is_integer() else f"{gb:.1f}GB"
    return f"{value}MB"


def evaluate_catalog_fit(entry: RAGModelCatalogEntry, profile) -> FitEvaluation:
    return _evaluate_catalog_fit(entry, profile)


def catalog_rows(profile, *, query: str = "") -> list[dict]:
    rows = []
    for index, entry in enumerate(search_rag_model_catalog(query), start=1):
        assessment = assess_catalog_entry(entry, profile)
        fit_evaluation = _evaluate_catalog_fit(entry, profile)
        estimate = assessment.memory_estimate
        placement = assessment.memory_placement
        # UNAVAILABLE entries have no memory data; skip pool_requirement calc
        pool_requirement = (
            max(placement.required_vram_per_gpu_mb, default=placement.required_ram_mb)
            if placement is not None
            else 0
        )
        device = "GPU" if assessment.backend_profile != "llamacpp-cpu" else "CPU"
        rows.append(
            {
                "index": index,
                # ── identity ──────────────────────────────────────────────────
                "id": entry.id,
                "model_id": entry.model_id,
                # ── display ───────────────────────────────────────────────────
                "model": entry.display_name,
                "display_name": entry.display_name,
                "quant": entry.artifact.quant,
                "size": f"{entry.model_metadata.parameter_count_b:g}B" if entry.model_metadata.parameter_count_b else "-",
                "device": device,
                # ── backend resolution (Stage 2) ───────────────────────────────
                "runtime": assessment.runtime,
                "backend": assessment.backend_profile,
                "backend_status": assessment.backend_status,
                "backend_reason_code": assessment.backend_reason_code,
                "backend_reason": assessment.backend_reason,
                "base_url": "",
                # ── memory (Stage 3) ───────────────────────────────────────────
                "min_mem": _mb_label(estimate.logical_total_memory_mb) if estimate else "-",
                "rec_mem": _mb_label(pool_requirement),
                "ram": _mb_label(placement.required_ram_mb) if placement else "-",
                "logical_total_memory_mb": estimate.logical_total_memory_mb if estimate else None,
                "required_ram_mb": placement.required_ram_mb if placement else None,
                "required_vram_per_gpu_mb": list(placement.required_vram_per_gpu_mb) if placement else [],
                "disk_required_mb": assessment.disk_required_mb,
                "memory_estimate": estimate.as_dict() if estimate else None,
                "memory_placement": placement.as_dict() if placement else None,
                "estimated_decode_tps": assessment.estimated_decode_tps,
                # ── fit evaluation (Stage 4) ───────────────────────────────────
                "fit_status": fit_evaluation.fit_status,
                "fit_reason_codes": fit_evaluation.fit_reason_codes,
                # ── eligibility ────────────────────────────────────────────────
                "catalog_status": fit_evaluation.eligibility.catalog_status,
                "auto_selectable": fit_evaluation.eligibility.auto_selectable,
                "manual_selectable": fit_evaluation.eligibility.manual_selectable,
                "recommended": fit_evaluation.recommended,
                "reason": fit_evaluation.summary,
                # ── extras ────────────────────────────────────────────────────
                "priority": entry.priority,
                "safety": entry.policy.safety,
                "speed": "fast" if device == "GPU" else "standard",
                "size_label": f"{entry.model_metadata.parameter_count_b:g}B" if entry.model_metadata.parameter_count_b else "-",
                "context_length": assessment.context_length,
                "concurrency": 1,
                "catalog_assessment": assessment.as_dict(),
                "description": entry.description,
                "notes": ", ".join(entry.policy.tags),
            }
        )
    return rows



__all__ = [
    "FitEvaluation",
    "RAGModelCatalogEntry",
    "catalog_rows",
    "evaluate_catalog_fit",
    "get_catalog_entry",
    "get_rag_model_catalog",
    "search_rag_model_catalog",
]
