from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


PriorityPreset = Literal["speed", "balanced", "quality"]
SelectionMode = Literal["automatic", "manual"]
SelectionStatus = Literal[
    "SELECTED",
    "RISKY_CONFIRMATION_REQUIRED",
    "MANUAL_SELECTION_REQUIRED",
    "INVALID_MANUAL_SELECTION",
    "NO_SELECTABLE_MODEL",
]


@dataclass(frozen=True)
class SelectionCandidate:
    entry: Any
    assessment: Any
    fit_evaluation: Any


@dataclass(frozen=True)
class SelectionResult:
    selection_status: SelectionStatus
    selection_mode: SelectionMode
    priority_preset: PriorityPreset
    selected_candidate: SelectionCandidate | None
    ranked_artifact_ids: list[str]
    reason_code: str
    risky_confirmed: bool = False


_PRECISION_RANK = {
    "f32": 8,
    "fp32": 8,
    "f16": 7,
    "fp16": 7,
    "bf16": 7,
    "bfloat16": 7,
    "q8_0": 6,
    "q6_k": 5,
    "q5_k_m": 4,
    "q4_k_m": 3,
    "q4_0": 3,
    "awq": 3,
    "awq-int4": 3,
    "gptq": 3,
    "gptq-int4": 3,
    "q3_k_m": 2,
    "q2_k": 1,
}


def _precision_rank(entry: Any) -> int:
    artifact = entry.artifact
    values = (artifact.quant, artifact.dtype, artifact.format)
    for value in values:
        normalized = str(value or "").strip().lower()
        if normalized in _PRECISION_RANK:
            return _PRECISION_RANK[normalized]
    return 0


def estimated_decode_tps(assessment: Any) -> float | None:
    direct = getattr(assessment, "estimated_decode_tps", None)
    if direct is not None:
        return float(direct)
    performance = getattr(assessment, "performance_estimate", None)
    nested = getattr(performance, "estimated_decode_tps", None)
    return float(nested) if nested is not None else None


def _resource_headroom_ratio(fit_evaluation: Any) -> float | None:
    checks = [
        getattr(fit_evaluation, "ram_check", None),
        *list(getattr(fit_evaluation, "vram_checks", []) or []),
        getattr(fit_evaluation, "disk_check", None),
    ]
    ratios = [
        float(check.available_mb) / float(check.required_mb)
        for check in checks
        if check is not None and check.required_mb > 0
    ]
    return min(ratios) if ratios else None


def _known_number(value: float | int | None) -> tuple[int, float]:
    return (0, 0.0) if value is None else (1, float(value))


# Reciprocal Rank Fusion damping constant. Standard default; not tuned
# against real catalog data yet.
_RRF_K = 60


def _rank_positions(
    candidates: list[SelectionCandidate], key_fn
) -> dict[str, int]:
    """0-indexed rank per candidate under key_fn (descending), tie-broken by
    artifact id ascending -- the input to Reciprocal Rank Fusion below."""
    ordered = sorted(candidates, key=lambda item: item.entry.id)
    ordered = sorted(ordered, key=key_fn, reverse=True)
    return {item.entry.id: index for index, item in enumerate(ordered)}


def _rrf_component(rank: int) -> float:
    return 1.0 / (_RRF_K + rank + 1)


def _tiebreak_key(candidate: SelectionCandidate) -> tuple:
    entry = candidate.entry
    headroom = _known_number(_resource_headroom_ratio(candidate.fit_evaluation))
    precision = _precision_rank(entry)
    max_context = int(entry.model_metadata.max_context_length)
    return headroom, precision, max_context


def _rank(candidates: list[SelectionCandidate]) -> list[SelectionCandidate]:
    """Rank candidates by a single, preset-independent score: Reciprocal Rank
    Fusion of decode-TPS rank and parameter-count rank (always "faster and
    bigger wins" -- there is no per-preset direction). Ties fall back to
    resource headroom, precision, then max context, then artifact id."""
    if not candidates:
        return []
    tps_ranks = _rank_positions(
        candidates,
        lambda item: _known_number(estimated_decode_tps(item.assessment)),
    )
    param_ranks = _rank_positions(
        candidates,
        lambda item: float(item.entry.model_metadata.parameter_count_b),
    )

    def sort_key(item: SelectionCandidate) -> tuple:
        rrf_score = _rrf_component(tps_ranks[item.entry.id]) + _rrf_component(
            param_ranks[item.entry.id]
        )
        return (rrf_score, *_tiebreak_key(item))

    # Stable sort preserves artifact ID ascending as the final tie-break.
    ranked = sorted(candidates, key=lambda item: item.entry.id)
    return sorted(ranked, key=sort_key, reverse=True)


def rank_manual_candidates(
    candidates: list[SelectionCandidate],
    priority_preset: PriorityPreset,
) -> tuple[list[SelectionCandidate], list[SelectionCandidate]]:
    """Split candidates into the same FIT/RISKY-ranked-first, non-selectable-last
    order that manual selection enforces, so display order and eligibility can
    never drift out of sync with `select_catalog_model`.

    priority_preset no longer affects ranking (see `_rank`) -- it is accepted
    only so callers can keep threading the value through for the resulting
    SelectionResult's audit label."""
    del priority_preset
    fit_candidates = []
    risky_candidates = []
    non_selectable = []
    for candidate in candidates:
        fit_status = candidate.fit_evaluation.fit_status
        if candidate.fit_evaluation.eligibility.manual_selectable and fit_status in {"FIT", "RISKY"}:
            (fit_candidates if fit_status == "FIT" else risky_candidates).append(candidate)
        else:
            non_selectable.append(candidate)
    ranked = _rank(fit_candidates) + _rank(risky_candidates)
    return ranked, non_selectable


def select_catalog_model(
    candidates: list[SelectionCandidate],
    priority_preset: PriorityPreset,
    *,
    selection_mode: SelectionMode = "automatic",
    selected_artifact_id: str | None = None,
    risky_confirmed: bool = False,
) -> SelectionResult:
    if selection_mode not in {"automatic", "manual"}:
        raise ValueError(f"Unknown selection_mode: {selection_mode}")
    if priority_preset not in {"speed", "balanced", "quality"}:
        raise ValueError(f"Unknown priority_preset: {priority_preset}")

    eligible = [
        candidate
        for candidate in candidates
        if (
            candidate.fit_evaluation.eligibility.auto_selectable
            if selection_mode == "automatic"
            else candidate.fit_evaluation.eligibility.manual_selectable
        )
        and candidate.fit_evaluation.fit_status in {"FIT", "RISKY"}
    ]
    fit_candidates = [
        candidate for candidate in eligible
        if candidate.fit_evaluation.fit_status == "FIT"
    ]
    risky_candidates = [
        candidate for candidate in eligible
        if candidate.fit_evaluation.fit_status == "RISKY"
    ]

    if selection_mode == "manual":
        ranked, _ = rank_manual_candidates(candidates, priority_preset)
        if not selected_artifact_id:
            return SelectionResult(
                selection_status="MANUAL_SELECTION_REQUIRED",
                selection_mode=selection_mode,
                priority_preset=priority_preset,
                selected_candidate=None,
                ranked_artifact_ids=[item.entry.id for item in ranked],
                reason_code="MANUAL_SELECTION_REQUIRED",
            )
        selected = next(
            (item for item in eligible if item.entry.id == selected_artifact_id),
            None,
        )
        if selected is None:
            return SelectionResult(
                selection_status="INVALID_MANUAL_SELECTION",
                selection_mode=selection_mode,
                priority_preset=priority_preset,
                selected_candidate=None,
                ranked_artifact_ids=[item.entry.id for item in ranked],
                reason_code="INVALID_MANUAL_SELECTION",
            )
        if selected.fit_evaluation.fit_status == "RISKY" and not risky_confirmed:
            return SelectionResult(
                selection_status="RISKY_CONFIRMATION_REQUIRED",
                selection_mode=selection_mode,
                priority_preset=priority_preset,
                selected_candidate=None,
                ranked_artifact_ids=[item.entry.id for item in ranked],
                reason_code="RISKY_CONFIRMATION_REQUIRED",
            )
        return SelectionResult(
            selection_status="SELECTED",
            selection_mode=selection_mode,
            priority_preset=priority_preset,
            selected_candidate=selected,
            ranked_artifact_ids=[item.entry.id for item in ranked],
            reason_code=(
                "SELECTED_MANUAL_RISKY"
                if selected.fit_evaluation.fit_status == "RISKY"
                else "SELECTED_MANUAL_FIT"
            ),
            risky_confirmed=(selected.fit_evaluation.fit_status == "RISKY"),
        )

    if fit_candidates:
        ranked = _rank(fit_candidates)
        selected = ranked[0]
        return SelectionResult(
            selection_status="SELECTED",
            selection_mode=selection_mode,
            priority_preset=priority_preset,
            selected_candidate=selected,
            ranked_artifact_ids=[item.entry.id for item in ranked],
            reason_code=f"SELECTED_FIT_BY_{priority_preset.upper()}",
        )

    ranked = _rank(risky_candidates)
    if ranked and not risky_confirmed:
        return SelectionResult(
            selection_status="RISKY_CONFIRMATION_REQUIRED",
            selection_mode=selection_mode,
            priority_preset=priority_preset,
            selected_candidate=None,
            ranked_artifact_ids=[item.entry.id for item in ranked],
            reason_code="RISKY_CONFIRMATION_REQUIRED",
        )
    if ranked:
        return SelectionResult(
            selection_status="SELECTED",
            selection_mode=selection_mode,
            priority_preset=priority_preset,
            selected_candidate=ranked[0],
            ranked_artifact_ids=[item.entry.id for item in ranked],
            reason_code=f"SELECTED_RISKY_BY_{priority_preset.upper()}",
            risky_confirmed=True,
        )
    return SelectionResult(
        selection_status="NO_SELECTABLE_MODEL",
        selection_mode=selection_mode,
        priority_preset=priority_preset,
        selected_candidate=None,
        ranked_artifact_ids=[],
        reason_code="NO_SELECTABLE_MODEL",
    )
