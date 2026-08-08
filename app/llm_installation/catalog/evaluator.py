from typing import Literal

from pydantic import BaseModel, Field

from llm_installation.catalog.models import RAGModelCatalogEntry
from llm_installation.runtime_probe import ServerRuntimeProfile


HardwareProfile = ServerRuntimeProfile


class CatalogEligibility(BaseModel):
    catalog_status: Literal["ACTIVE", "DISABLED", "BLOCKED"]
    visible: bool
    auto_select_allowed: bool
    manual_select_allowed: bool
    reason_code: str
    auto_selection_reason_codes: list[str] = Field(default_factory=list)
    manual_selection_reason_codes: list[str] = Field(default_factory=list)
    auto_selectable: bool
    manual_selectable: bool


class PoolCheck(BaseModel):
    required_mb: int
    available_mb: int
    hard_fit: bool
    soft_fit: bool


class VramPoolCheck(PoolCheck):
    device_index: int


class FitEvaluation(BaseModel):
    fit_status: Literal["FIT", "RISKY", "NOFIT"]
    fit_reason_codes: list[str] = Field(default_factory=list)
    source_reason_code: str | None = None
    summary: str
    ram_check: PoolCheck | None = None
    vram_checks: list[VramPoolCheck] = Field(default_factory=list)
    disk_check: PoolCheck | None = None
    eligibility: CatalogEligibility
    warnings: list[str] = Field(default_factory=list)

    @property
    def recommended(self) -> bool:
        return self.fit_status == "FIT" and self.eligibility.auto_selectable


def _catalog_eligibility(entry: RAGModelCatalogEntry, fit_status: str) -> CatalogEligibility:
    if entry.policy.safety == "danger":
        catalog_status = "BLOCKED"
        reason_code = "SAFETY_BLOCKED"
    elif not entry.enabled:
        catalog_status = "DISABLED"
        reason_code = "CATALOG_DISABLED"
    else:
        catalog_status = "ACTIVE"
        reason_code = "CATALOG_ACTIVE"

    common_reasons = []
    if catalog_status != "ACTIVE":
        common_reasons.append("CATALOG_NOT_ACTIVE")
    if not entry.policy.visible:
        common_reasons.append("HIDDEN_BY_POLICY")
    if fit_status == "NOFIT":
        common_reasons.append("RESOURCE_NOFIT")

    auto_reasons = list(common_reasons)
    if not entry.policy.allow_auto_select:
        auto_reasons.append("AUTO_SELECTION_DISABLED")

    manual_reasons = list(common_reasons)
    if not entry.policy.allow_manual_select:
        manual_reasons.append("MANUAL_SELECTION_DISABLED")

    return CatalogEligibility(
        catalog_status=catalog_status,
        visible=entry.policy.visible,
        auto_select_allowed=entry.policy.allow_auto_select,
        manual_select_allowed=entry.policy.allow_manual_select,
        reason_code=reason_code,
        auto_selection_reason_codes=auto_reasons,
        manual_selection_reason_codes=manual_reasons,
        auto_selectable=not auto_reasons,
        manual_selectable=not manual_reasons,
    )


def evaluate_catalog_fit(
    entry: RAGModelCatalogEntry,
    profile: HardwareProfile,
) -> FitEvaluation:
    from llm_installation.planner import (
        DISK_HEADROOM_MULTIPLIER,
        HEADROOM_MULTIPLIER,
        _gpu_free_list,
        _planning_ram,
        assess_catalog_entry,
    )

    assessment = assess_catalog_entry(entry, profile)
    fit_status = assessment.fit_status

    ram_check: PoolCheck | None = None
    vram_checks: list[VramPoolCheck] = []
    disk_check: PoolCheck | None = None

    if assessment.memory_placement is not None:
        available_ram = _planning_ram(profile)
        required_ram = assessment.memory_placement.required_ram_mb
        ram_check = PoolCheck(
            required_mb=required_ram,
            available_mb=available_ram,
            hard_fit=required_ram <= available_ram,
            soft_fit=(required_ram * HEADROOM_MULTIPLIER) <= available_ram,
        )

        free_vram = _gpu_free_list(profile)
        for idx, required_vram in enumerate(
            assessment.memory_placement.required_vram_per_gpu_mb
        ):
            available_vram = free_vram[idx] if idx < len(free_vram) else 0
            vram_checks.append(
                VramPoolCheck(
                    device_index=idx,
                    required_mb=required_vram,
                    available_mb=available_vram,
                    hard_fit=required_vram <= available_vram,
                    soft_fit=(required_vram * HEADROOM_MULTIPLIER) <= available_vram,
                )
            )

    if assessment.disk_required_mb is not None:
        disk_free = int(getattr(profile, "disk_free_mb", 0) or 0)
        disk_req = assessment.disk_required_mb
        disk_check = PoolCheck(
            required_mb=disk_req,
            available_mb=disk_free,
            hard_fit=disk_req <= disk_free,
            soft_fit=(disk_req * DISK_HEADROOM_MULTIPLIER) <= disk_free,
        )

    return FitEvaluation(
        fit_status=fit_status,
        fit_reason_codes=list(getattr(assessment, "fit_reason_codes", []) or []),
        summary=assessment.fit_reason,
        ram_check=ram_check,
        vram_checks=vram_checks,
        disk_check=disk_check,
        eligibility=_catalog_eligibility(entry, fit_status),
        warnings=list(getattr(assessment, "warnings", []) or []),
    )
