from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from workspaces.models import WorkspaceQualityProfileRevision


SCHEMA_VERSION = 1

RETRIEVAL_SCHEMA: dict[str, dict[str, Any]] = {
    "dense_weight": {"type": "number", "tier": "core", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "effects": ["retrieval_quality"]},
    "sparse_weight": {"type": "number", "tier": "core", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "effects": ["retrieval_quality"]},
    "search_top_k": {"type": "integer", "tier": "core", "minimum": 1, "maximum": 50, "step": 1, "effects": ["retrieval_quality", "latency"]},
    "rag_search_top_k": {"type": "integer", "tier": "core", "minimum": 1, "maximum": 10, "step": 1, "effects": ["context_quality", "latency"]},
    "retrieval_threshold": {"type": "number", "tier": "core", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "effects": ["retrieval_quality"], "nullable": True},
    "evidence_top_k": {"type": "integer", "tier": "core", "minimum": 1, "maximum": 10, "step": 1, "effects": ["context_quality"]},
    "evidence_context_window": {"type": "integer", "tier": "core", "minimum": 0, "maximum": 3, "step": 1, "effects": ["context_quality", "latency"]},
    "candidate_multiplier": {"type": "integer", "tier": "advanced", "minimum": 1, "maximum": 50, "step": 1, "effects": ["retrieval_quality", "latency"]},
    "per_node_candidate_cap": {"type": "integer", "tier": "advanced", "minimum": 1, "maximum": 20, "step": 1, "effects": ["retrieval_quality"]},
    "query_sparse_top_n": {"type": "integer", "tier": "advanced", "minimum": 1, "maximum": 256, "step": 1, "effects": ["retrieval_quality", "latency"]},
    "pooling_method": {"type": "enum", "tier": "advanced", "choices": ["normalized_logsumexp", "normalized_softmax", "max"], "effects": ["retrieval_quality"]},
    "pool_top_k": {"type": "integer", "tier": "advanced", "minimum": 1, "maximum": 20, "step": 1, "effects": ["retrieval_quality"]},
    "pool_tau": {"type": "number", "tier": "advanced", "minimum": 0.1, "maximum": 20.0, "step": 0.1, "effects": ["retrieval_quality"]},
    "doc_length_penalty_alpha": {"type": "number", "tier": "advanced", "minimum": 0.0, "maximum": 1.0, "step": 0.05, "effects": ["retrieval_quality"]},
    "contextual_compression": {"type": "boolean", "tier": "advanced", "effects": ["context_quality", "latency"]},
}

GENERATION_SCHEMA: dict[str, dict[str, Any]] = {
    "max_output_tokens": {"type": "integer", "tier": "core", "minimum": 64, "maximum": 8192, "step": 64, "effects": ["generation_quality", "latency"]},
    "temperature": {"type": "number", "tier": "core", "minimum": 0.0, "maximum": 2.0, "step": 0.05, "effects": ["generation_quality"]},
    "top_p": {"type": "number", "tier": "advanced", "minimum": 0.01, "maximum": 1.0, "step": 0.01, "effects": ["generation_quality"]},
}


@dataclass
class RetrievalProfileError(Exception):
    code: str
    message: str
    status: int
    details: dict[str, Any] | None = None

    def __str__(self):
        return self.message


def retrieval_defaults() -> dict[str, Any]:
    return {
        "dense_weight": float(getattr(settings, "EMBEDDING_HYBRID_DENSE_WEIGHT", 0.3)),
        "sparse_weight": float(getattr(settings, "EMBEDDING_HYBRID_SPARSE_WEIGHT", 0.7)),
        "search_top_k": 5,
        "rag_search_top_k": int(getattr(settings, "RAG_SEARCH_TOP_K", 3)),
        "retrieval_threshold": getattr(settings, "RAG_RETRIEVAL_THRESHOLD", None),
        "evidence_top_k": int(getattr(settings, "EMBEDDING_EVIDENCE_TOP_K", 3)),
        "evidence_context_window": int(getattr(settings, "EMBEDDING_EVIDENCE_CONTEXT_WINDOW", 1)),
        "candidate_multiplier": int(getattr(settings, "EMBEDDING_HYBRID_CANDIDATE_MULTIPLIER", 12)),
        "per_node_candidate_cap": int(getattr(settings, "EMBEDDING_PER_NODE_CANDIDATE_CAP", 4)),
        "query_sparse_top_n": int(getattr(settings, "EMBEDDING_QUERY_SPARSE_TOP_N", 32)),
        "pooling_method": getattr(settings, "EMBEDDING_DOC_POOLING_METHOD", "normalized_logsumexp"),
        "pool_top_k": int(getattr(settings, "EMBEDDING_DOC_POOL_TOP_K", 5)),
        "pool_tau": float(getattr(settings, "EMBEDDING_DOC_POOL_TAU", 5.0)),
        "doc_length_penalty_alpha": float(getattr(settings, "EMBEDDING_DOC_LENGTH_PENALTY_ALPHA", 0.1)),
        "contextual_compression": {"enabled": bool(getattr(settings, "CONTEXTUAL_COMPRESSION_ENABLED", False))},
    }


def generation_defaults() -> dict[str, Any]:
    return {
        "max_output_tokens": int(getattr(settings, "RAG_MAX_TOKENS", 512)),
        "temperature": float(getattr(settings, "RAG_TEMPERATURE", 0.2)),
        "top_p": float(getattr(settings, "RAG_TOP_P", 0.9)),
    }


def _effective(overrides: dict[str, Any] | None) -> dict[str, Any]:
    effective = retrieval_defaults()
    effective.update(overrides or {})
    return effective


def _effective_generation(overrides: dict[str, Any] | None) -> dict[str, Any]:
    effective = generation_defaults()
    effective.update(overrides or {})
    return effective


def _same(left: Any, right: Any) -> bool:
    return left == right


def _validate_value(name: str, value: Any, spec: dict[str, Any], errors: dict[str, list[str]]):
    if value is None and spec.get("nullable"):
        return
    kind = spec["type"]
    if kind == "integer":
        valid_type = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "number":
        valid_type = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
    elif kind == "boolean":
        valid_type = isinstance(value, dict) and isinstance(value.get("enabled"), bool) and set(value) == {"enabled"}
    elif kind == "enum":
        valid_type = isinstance(value, str) and value in spec["choices"]
    else:
        valid_type = False
    if not valid_type:
        errors.setdefault(name, []).append(f"Invalid {kind} value.")
        return
    if kind in {"integer", "number"}:
        if value < spec["minimum"] or value > spec["maximum"]:
            errors.setdefault(name, []).append(
                f"Must be between {spec['minimum']} and {spec['maximum']}."
            )


def validate_retrieval_config(config: dict[str, Any]) -> list[str]:
    errors: dict[str, list[str]] = {}
    unknown = sorted(set(config) - set(RETRIEVAL_SCHEMA))
    for name in unknown:
        errors.setdefault(name, []).append("Unknown retrieval setting.")
    for name, spec in RETRIEVAL_SCHEMA.items():
        if name not in config:
            errors.setdefault(name, []).append("This setting is required.")
        else:
            _validate_value(name, config[name], spec, errors)
    if not errors:
        if abs(float(config["dense_weight"]) + float(config["sparse_weight"]) - 1.0) > 1e-6:
            errors["weights"] = ["dense_weight and sparse_weight must add up to 1.0."]
        if config["pooling_method"] == "max" and config["pool_tau"] != retrieval_defaults()["pool_tau"]:
            errors["pool_tau"] = ["pool_tau cannot be overridden when pooling_method is max."]
    if errors:
        raise RetrievalProfileError(
            "PROFILE_VALIDATION_FAILED",
            "Retrieval profile validation failed.",
            400,
            errors,
        )
    warnings = []
    if config["evidence_top_k"] > config["pool_top_k"]:
        warnings.append("evidence_top_k is greater than pool_top_k.")
    return warnings


def validate_generation_config(config: dict[str, Any]) -> list[str]:
    errors: dict[str, list[str]] = {}
    unknown = sorted(set(config) - set(GENERATION_SCHEMA))
    for name in unknown:
        errors.setdefault(name, []).append("Unknown generation setting.")
    for name, spec in GENERATION_SCHEMA.items():
        if name not in config:
            errors.setdefault(name, []).append("This setting is required.")
        else:
            _validate_value(name, config[name], spec, errors)
    if errors:
        raise RetrievalProfileError(
            "PROFILE_VALIDATION_FAILED",
            "Generation profile validation failed.",
            400,
            errors,
        )
    return []


def _sparse_overrides(config: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in config.items() if not _same(value, defaults[key])}


def _active_for_workspace(workspace, *, actor=None, lock=False):
    query = WorkspaceQualityProfileRevision.objects.filter(
        workspace=workspace,
        status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
    )
    if lock:
        active = query.select_for_update().first()
    else:
        active = query.select_related("created_by", "based_on").first()
    if active is None:
        active = WorkspaceQualityProfileRevision.objects.create(
            workspace=workspace,
            version=1,
            revision=1,
            status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
            change_axis=WorkspaceQualityProfileRevision.AXIS_RETRIEVAL,
            retrieval_config={},
            validation_state="verified",
            created_by=actor,
            applied_at=timezone.now(),
            note="Server defaults",
        )
    return active


def get_effective_retrieval_config(workspace) -> dict[str, Any]:
    if workspace is None:
        return retrieval_defaults()
    active = WorkspaceQualityProfileRevision.objects.filter(
        workspace=workspace,
        status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
    ).only("retrieval_config").first()
    return _effective(active.retrieval_config if active else {})


def get_effective_generation_config(workspace) -> dict[str, Any]:
    if workspace is None:
        return generation_defaults()
    active = WorkspaceQualityProfileRevision.objects.filter(
        workspace=workspace,
        status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
    ).only("generation_config").first()
    return _effective_generation(active.generation_config if active else {})


def retrieval_tuning_params(config: dict[str, Any]) -> dict[str, Any]:
    excluded = {"search_top_k", "rag_search_top_k", "retrieval_threshold"}
    return {key: value for key, value in config.items() if key not in excluded}


def _serialize_revision_generic(revision, *, field_name, defaults_fn, schema, active=None) -> dict[str, Any]:
    config = getattr(revision, field_name)
    effective = defaults_fn()
    effective.update(config or {})
    if revision.status == WorkspaceQualityProfileRevision.STATUS_DRAFT and active is not None:
        active_effective = defaults_fn()
        active_effective.update(getattr(active, field_name) or {})
        changed_fields = [key for key in schema if not _same(effective[key], active_effective[key])]
    else:
        changed_fields = list(config)
    creator = revision.created_by
    return {
        "uid": str(revision.uid),
        "version": revision.version,
        "revision": revision.revision,
        "status": revision.status,
        "change_axis": revision.change_axis,
        "based_on_uid": str(revision.based_on.uid) if revision.based_on_id else None,
        "overrides": config,
        "effective": effective,
        "changed_fields": changed_fields,
        "validation": {
            "state": revision.validation_state,
            "last_run_uid": str(revision.applied_evaluation_run_uid) if revision.applied_evaluation_run_uid else None,
            "warnings": revision.validation_warnings,
        },
        "created_by": ({"id": creator.id, "display_name": creator.display_name} if creator else None),
        "created_at": revision.created_at.isoformat(),
        "updated_at": revision.updated_at.isoformat(),
        "applied_at": revision.applied_at.isoformat() if revision.applied_at else None,
        "note": revision.note,
    }


def _serialize_revision(revision, *, active=None) -> dict[str, Any]:
    return _serialize_revision_generic(revision, field_name="retrieval_config", defaults_fn=retrieval_defaults, schema=RETRIEVAL_SCHEMA, active=active)


def _serialize_generation_revision(revision, *, active=None) -> dict[str, Any]:
    return _serialize_revision_generic(revision, field_name="generation_config", defaults_fn=generation_defaults, schema=GENERATION_SCHEMA, active=active)


def _profile_envelope(workspace, *, actor, can_edit, axis, defaults_fn, schema, serialize_fn) -> dict[str, Any]:
    with transaction.atomic():
        from workspaces.models import Workspace

        locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
        active = _active_for_workspace(locked_workspace, actor=actor)
    draft = WorkspaceQualityProfileRevision.objects.filter(
        workspace=workspace,
        status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
    ).select_related("created_by", "based_on").first()
    axis_draft = draft if draft and draft.change_axis == axis else None
    return {
        "ok": True,
        "workspace_uid": str(workspace.uid),
        "axis": axis,
        "active": serialize_fn(active),
        "draft": serialize_fn(axis_draft, active=active) if axis_draft else None,
        "defaults": defaults_fn(),
        "schema": schema,
        "capabilities": {"schema_version": SCHEMA_VERSION},
        "permissions": {"can_read": True, "can_edit": can_edit, "can_apply": can_edit},
        "draft_conflict": (
            {"change_axis": draft.change_axis, "uid": str(draft.uid)}
            if draft and axis_draft is None else None
        ),
    }


def retrieval_profile_envelope(workspace, *, actor, can_edit=True) -> dict[str, Any]:
    return _profile_envelope(
        workspace,
        actor=actor,
        can_edit=can_edit,
        axis=WorkspaceQualityProfileRevision.AXIS_RETRIEVAL,
        defaults_fn=retrieval_defaults,
        schema=RETRIEVAL_SCHEMA,
        serialize_fn=_serialize_revision,
    )


def generation_profile_envelope(workspace, *, actor, can_edit=True) -> dict[str, Any]:
    return _profile_envelope(
        workspace,
        actor=actor,
        can_edit=can_edit,
        axis=WorkspaceQualityProfileRevision.AXIS_GENERATION,
        defaults_fn=generation_defaults,
        schema=GENERATION_SCHEMA,
        serialize_fn=_serialize_generation_revision,
    )


def list_quality_profile_versions(workspace, *, axis: str | None = None, page: int = 1, limit: int = 20) -> dict[str, Any]:
    queryset = WorkspaceQualityProfileRevision.objects.filter(
        workspace=workspace,
    ).exclude(status=WorkspaceQualityProfileRevision.STATUS_DRAFT).select_related("created_by")
    if axis:
        queryset = queryset.filter(change_axis=axis)
    start = max(page, 1) - 1
    start *= limit
    results = []
    for revision in queryset[start:start + limit]:
        creator = revision.created_by
        results.append({
            "uid": str(revision.uid),
            "version": revision.version,
            "change_axis": revision.change_axis,
            "validation": {"state": revision.validation_state},
            "note": revision.note,
            "applied_at": revision.applied_at.isoformat() if revision.applied_at else None,
            "created_by": ({"id": creator.id, "display_name": creator.display_name} if creator else None),
        })
    return {"ok": True, "results": results}


def profile_threshold_to_retriever(threshold: float | None) -> float | None:
    """Translate the profile's normalized minimum relevance into the active store contract."""
    if threshold is None:
        return None
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    strategy = get_active_embedding_runtime().distance_strategy
    if strategy == "cosine":
        return 1.0 - threshold
    if strategy == "l2":
        return None if threshold <= 0 else (1.0 / threshold) - 1.0
    return threshold


def save_retrieval_draft(workspace, *, actor, expected_revision, overrides, reset_fields, note):
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise RetrievalProfileError("INVALID_REQUEST", "expected_revision must be a non-negative integer.", 400)
    if not isinstance(overrides, dict) or not isinstance(reset_fields, list) or not all(isinstance(item, str) for item in reset_fields):
        raise RetrievalProfileError("INVALID_REQUEST", "overrides must be an object and reset_fields must be a string array.", 400)
    if not isinstance(note, str) or len(note) > 500:
        raise RetrievalProfileError("INVALID_REQUEST", "note must be at most 500 characters.", 400)
    duplicate_fields = sorted(set(overrides) & set(reset_fields))
    unknown_fields = sorted((set(overrides) | set(reset_fields)) - set(RETRIEVAL_SCHEMA))
    if duplicate_fields or unknown_fields:
        raise RetrievalProfileError(
            "INVALID_REQUEST",
            "Invalid retrieval profile fields.",
            400,
            {"duplicate_fields": duplicate_fields, "unknown_fields": unknown_fields},
        )

    with transaction.atomic():
        from workspaces.models import Workspace

        locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
        active = _active_for_workspace(locked_workspace, actor=actor, lock=True)
        draft = WorkspaceQualityProfileRevision.objects.select_for_update().filter(
            workspace=locked_workspace,
            status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
        ).first()
        if draft and draft.change_axis != WorkspaceQualityProfileRevision.AXIS_RETRIEVAL:
            raise RetrievalProfileError("DRAFT_AXIS_CONFLICT", "Another quality profile axis already has a draft.", 409, {"change_axis": draft.change_axis, "uid": str(draft.uid)})
        current_revision = draft.revision if draft else 0
        if expected_revision != current_revision:
            raise RetrievalProfileError("PROFILE_REVISION_CONFLICT", "The draft was changed by another request.", 409, {"expected_revision": expected_revision, "current_revision": current_revision})

        defaults = retrieval_defaults()
        candidate = _effective(draft.retrieval_config if draft else active.retrieval_config)
        candidate.update(overrides)
        for field in reset_fields:
            candidate[field] = defaults[field]
        warnings = validate_retrieval_config(candidate)
        sparse = _sparse_overrides(candidate, defaults)
        active_effective = _effective(active.retrieval_config)
        if all(_same(candidate[key], active_effective[key]) for key in RETRIEVAL_SCHEMA):
            raise RetrievalProfileError("PROFILE_HAS_NO_CHANGES", "The retrieval profile has no changes.", 409)
        if draft is None:
            draft = WorkspaceQualityProfileRevision.objects.create(
                workspace=locked_workspace,
                version=active.version + 1,
                revision=1,
                status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
                change_axis=WorkspaceQualityProfileRevision.AXIS_RETRIEVAL,
                retrieval_config=sparse,
                generation_config=active.generation_config,
                prompt_policy=active.prompt_policy,
                validation_state="not_run",
                validation_warnings=warnings,
                based_on=active,
                created_by=actor,
                note=note.strip(),
            )
        else:
            draft.revision += 1
            draft.retrieval_config = sparse
            draft.validation_state = "not_run"
            draft.validation_warnings = warnings
            draft.applied_evaluation_run_uid = None
            draft.note = note.strip()
            draft.save(update_fields=["revision", "retrieval_config", "validation_state", "validation_warnings", "applied_evaluation_run_uid", "note", "updated_at"])
        return {"ok": True, "draft": _serialize_revision(draft, active=active)}


def discard_retrieval_draft(workspace, *, expected_revision):
    with transaction.atomic():
        draft = WorkspaceQualityProfileRevision.objects.select_for_update().filter(
            workspace=workspace,
            status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
            change_axis=WorkspaceQualityProfileRevision.AXIS_RETRIEVAL,
        ).first()
        current_revision = draft.revision if draft else 0
        if draft is None or expected_revision != current_revision:
            raise RetrievalProfileError("PROFILE_REVISION_CONFLICT", "The draft was changed by another request.", 409, {"expected_revision": expected_revision, "current_revision": current_revision})
        draft.delete()
    return {"ok": True, "draft": None}


def apply_retrieval_draft(workspace, *, actor, expected_revision, evaluation_run_uid, allow_unverified, note):
    if not isinstance(allow_unverified, bool):
        raise RetrievalProfileError("INVALID_REQUEST", "allow_unverified must be a boolean.", 400)
    if not evaluation_run_uid and not allow_unverified:
        raise RetrievalProfileError("EVALUATION_REQUIRED", "A successful evaluation run is required before apply.", 409)
    if not isinstance(note, str) or not note.strip() or len(note) > 500:
        raise RetrievalProfileError("INVALID_REQUEST", "A note of at most 500 characters is required for unverified apply.", 400)

    with transaction.atomic():
        from workspaces.models import Workspace

        locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
        active = _active_for_workspace(locked_workspace, actor=actor, lock=True)
        draft = WorkspaceQualityProfileRevision.objects.select_for_update().filter(
            workspace=locked_workspace,
            status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
            change_axis=WorkspaceQualityProfileRevision.AXIS_RETRIEVAL,
        ).first()
        current_revision = draft.revision if draft else 0
        if draft is None or expected_revision != current_revision:
            raise RetrievalProfileError("PROFILE_REVISION_CONFLICT", "The draft was changed by another request.", 409, {"expected_revision": expected_revision, "current_revision": current_revision})
        validate_retrieval_config(_effective(draft.retrieval_config))
        if evaluation_run_uid:
            from document_ai.search.evaluation import resolve_verified_evaluation_run

            run = resolve_verified_evaluation_run(
                locked_workspace,
                axis=WorkspaceQualityProfileRevision.AXIS_RETRIEVAL,
                draft=draft,
                evaluation_run_uid=evaluation_run_uid,
            )
            validation_state = "verified"
            draft.applied_evaluation_run_uid = run.uid
        else:
            validation_state = "unverified"
        draft.generation_config = active.generation_config
        draft.prompt_policy = active.prompt_policy
        active.status = WorkspaceQualityProfileRevision.STATUS_ARCHIVED
        active.save(update_fields=["status", "updated_at"])
        draft.status = WorkspaceQualityProfileRevision.STATUS_ACTIVE
        draft.validation_state = validation_state
        draft.note = note.strip()
        draft.applied_at = timezone.now()
        draft.save(update_fields=["generation_config", "prompt_policy", "applied_evaluation_run_uid", "status", "validation_state", "note", "applied_at", "updated_at"])
    return retrieval_profile_envelope(workspace, actor=actor)


def save_generation_draft(workspace, *, actor, expected_revision, overrides, reset_fields, note):
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise RetrievalProfileError("INVALID_REQUEST", "expected_revision must be a non-negative integer.", 400)
    if not isinstance(overrides, dict) or not isinstance(reset_fields, list) or not all(isinstance(item, str) for item in reset_fields):
        raise RetrievalProfileError("INVALID_REQUEST", "overrides must be an object and reset_fields must be a string array.", 400)
    if not isinstance(note, str) or len(note) > 500:
        raise RetrievalProfileError("INVALID_REQUEST", "note must be at most 500 characters.", 400)
    duplicate_fields = sorted(set(overrides) & set(reset_fields))
    unknown_fields = sorted((set(overrides) | set(reset_fields)) - set(GENERATION_SCHEMA))
    if duplicate_fields or unknown_fields:
        raise RetrievalProfileError(
            "INVALID_REQUEST",
            "Invalid generation profile fields.",
            400,
            {"duplicate_fields": duplicate_fields, "unknown_fields": unknown_fields},
        )

    with transaction.atomic():
        from workspaces.models import Workspace

        locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
        active = _active_for_workspace(locked_workspace, actor=actor, lock=True)
        draft = WorkspaceQualityProfileRevision.objects.select_for_update().filter(
            workspace=locked_workspace,
            status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
        ).first()
        if draft and draft.change_axis != WorkspaceQualityProfileRevision.AXIS_GENERATION:
            raise RetrievalProfileError("DRAFT_AXIS_CONFLICT", "Another quality profile axis already has a draft.", 409, {"change_axis": draft.change_axis, "uid": str(draft.uid)})
        current_revision = draft.revision if draft else 0
        if expected_revision != current_revision:
            raise RetrievalProfileError("PROFILE_REVISION_CONFLICT", "The draft was changed by another request.", 409, {"expected_revision": expected_revision, "current_revision": current_revision})

        defaults = generation_defaults()
        candidate = _effective_generation(draft.generation_config if draft else active.generation_config)
        candidate.update(overrides)
        for field in reset_fields:
            candidate[field] = defaults[field]
        validate_generation_config(candidate)
        sparse = _sparse_overrides(candidate, defaults)
        active_effective = _effective_generation(active.generation_config)
        if all(_same(candidate[key], active_effective[key]) for key in GENERATION_SCHEMA):
            raise RetrievalProfileError("PROFILE_HAS_NO_CHANGES", "The generation profile has no changes.", 409)
        if draft is None:
            draft = WorkspaceQualityProfileRevision.objects.create(
                workspace=locked_workspace,
                version=active.version + 1,
                revision=1,
                status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
                change_axis=WorkspaceQualityProfileRevision.AXIS_GENERATION,
                retrieval_config=active.retrieval_config,
                generation_config=sparse,
                prompt_policy=active.prompt_policy,
                validation_state="not_run",
                validation_warnings=[],
                based_on=active,
                created_by=actor,
                note=note.strip(),
            )
        else:
            draft.revision += 1
            draft.generation_config = sparse
            draft.validation_state = "not_run"
            draft.validation_warnings = []
            draft.applied_evaluation_run_uid = None
            draft.note = note.strip()
            draft.save(update_fields=["revision", "generation_config", "validation_state", "validation_warnings", "applied_evaluation_run_uid", "note", "updated_at"])
        return {"ok": True, "draft": _serialize_generation_revision(draft, active=active)}


def discard_generation_draft(workspace, *, expected_revision):
    with transaction.atomic():
        draft = WorkspaceQualityProfileRevision.objects.select_for_update().filter(
            workspace=workspace,
            status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
            change_axis=WorkspaceQualityProfileRevision.AXIS_GENERATION,
        ).first()
        current_revision = draft.revision if draft else 0
        if draft is None or expected_revision != current_revision:
            raise RetrievalProfileError("PROFILE_REVISION_CONFLICT", "The draft was changed by another request.", 409, {"expected_revision": expected_revision, "current_revision": current_revision})
        draft.delete()
    return {"ok": True, "draft": None}


def apply_generation_draft(workspace, *, actor, expected_revision, evaluation_run_uid, allow_unverified, note):
    if not isinstance(allow_unverified, bool):
        raise RetrievalProfileError("INVALID_REQUEST", "allow_unverified must be a boolean.", 400)
    if evaluation_run_uid:
        raise RetrievalProfileError("EVALUATION_REQUIRED", "Generation evaluation runs are not available yet.", 409)
    if not allow_unverified:
        raise RetrievalProfileError("EVALUATION_REQUIRED", "A successful evaluation run is required before apply.", 409)
    if not isinstance(note, str) or not note.strip() or len(note) > 500:
        raise RetrievalProfileError("INVALID_REQUEST", "A note of at most 500 characters is required for unverified apply.", 400)

    with transaction.atomic():
        from workspaces.models import Workspace

        locked_workspace = Workspace.objects.select_for_update().get(pk=workspace.pk)
        active = _active_for_workspace(locked_workspace, actor=actor, lock=True)
        draft = WorkspaceQualityProfileRevision.objects.select_for_update().filter(
            workspace=locked_workspace,
            status=WorkspaceQualityProfileRevision.STATUS_DRAFT,
            change_axis=WorkspaceQualityProfileRevision.AXIS_GENERATION,
        ).first()
        current_revision = draft.revision if draft else 0
        if draft is None or expected_revision != current_revision:
            raise RetrievalProfileError("PROFILE_REVISION_CONFLICT", "The draft was changed by another request.", 409, {"expected_revision": expected_revision, "current_revision": current_revision})
        validate_generation_config(_effective_generation(draft.generation_config))
        draft.retrieval_config = active.retrieval_config
        draft.prompt_policy = active.prompt_policy
        active.status = WorkspaceQualityProfileRevision.STATUS_ARCHIVED
        active.save(update_fields=["status", "updated_at"])
        draft.status = WorkspaceQualityProfileRevision.STATUS_ACTIVE
        draft.validation_state = "unverified"
        draft.note = note.strip()
        draft.applied_at = timezone.now()
        draft.save(update_fields=["retrieval_config", "prompt_policy", "status", "validation_state", "note", "applied_at", "updated_at"])
    return generation_profile_envelope(workspace, actor=actor)
