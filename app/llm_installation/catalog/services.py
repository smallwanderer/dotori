from pydantic import BaseModel

from llm_installation.catalog.evaluator import FitEvaluation, HardwareProfile, evaluate_catalog_fit
from llm_installation.catalog.loader import get_rag_model_catalog
from llm_installation.catalog.models import RAGModelCatalogEntry
from llm_installation.planner import assess_catalog_entry


class RAGModelCatalogItemView(BaseModel):
    id: str
    display_name: str
    description: str

    hf_repo_id: str
    artifact_format: str
    quant: str

    runtime: str
    backend: str
    device: str
    context_length: int
    parallel: int

    min_ram_mb: int
    rec_ram_mb: int
    min_vram_mb: int
    rec_vram_mb: int
    logical_total_memory_mb: int
    required_ram_mb: int
    required_vram_per_gpu_mb: list[int]
    disk_required_mb: int | None

    capabilities: list[str]
    tags: list[str]

    fit_evaluation: FitEvaluation


def build_catalog_item_view(
    entry: RAGModelCatalogEntry,
    profile: HardwareProfile,
) -> RAGModelCatalogItemView:
    fit = evaluate_catalog_fit(entry, profile)
    assessment = assess_catalog_entry(entry, profile)
    estimate = assessment.memory_estimate
    placement = assessment.memory_placement

    return RAGModelCatalogItemView(
        id=entry.id,
        display_name=entry.display_name,
        description=entry.description,

        hf_repo_id=entry.hf.repo_id,
        artifact_format=entry.artifact.format,
        quant=entry.artifact.quant,

        runtime=assessment.runtime,
        backend=assessment.backend_profile,
        device=entry.device_label,
        context_length=assessment.context_length,
        parallel=1,

        min_ram_mb=placement.base_ram_mb,
        rec_ram_mb=placement.required_ram_mb,
        min_vram_mb=max(placement.base_vram_per_gpu_mb, default=0),
        rec_vram_mb=max(placement.required_vram_per_gpu_mb, default=0),
        logical_total_memory_mb=estimate.logical_total_memory_mb,
        required_ram_mb=placement.required_ram_mb,
        required_vram_per_gpu_mb=list(placement.required_vram_per_gpu_mb),
        disk_required_mb=assessment.disk_required_mb,

        capabilities=entry.capabilities.tasks,
        tags=entry.policy.tags,

        fit_evaluation=fit,
    )


def list_catalog_for_hardware(
    profile: HardwareProfile,
) -> list[RAGModelCatalogItemView]:
    entries = get_rag_model_catalog()

    views = [
        build_catalog_item_view(entry, profile)
        for entry in entries
        if entry.policy.visible
    ]

    return sorted(
        views,
        key=lambda item: (
            item.fit_evaluation.fit_status != "FIT",
            -_priority_of_status(item.fit_evaluation.fit_status),
        ),
    )


def _priority_of_status(status: str) -> int:
    return {
        "FIT": 4,
        "RISKY": 3,
        "NOFIT": 2,
    }.get(status, 0)
