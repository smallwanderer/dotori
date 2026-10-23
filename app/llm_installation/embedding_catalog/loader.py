from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from llm_installation.embedding_catalog.models import (
    EmbeddingCatalogEntry,
    EmbeddingModelEntry,
    EmbeddingProfileEntry,
)
from llm_installation.embedding_catalog.presets import EMBEDDING_PRESETS
from llm_installation.function_catalog import (
    validate_function_evidence_target,
    validate_function_list,
)


CATALOG_DIR = Path(__file__).parent
MODELS_DIR = CATALOG_DIR / "models"
PROFILES_DIR = CATALOG_DIR / "profiles"

from capabilities.providers import EMBEDDING_BACKENDS

KNOWN_PROVIDERS = frozenset(EMBEDDING_BACKENDS)
KNOWN_STORES = {
    "pgvector_chunk_1024": 1024,
    "pgvector_chunk_640": 640,
    "pgvector_chunk_768": 768,
    "pgvector_chunk_1536": 1536,
    "pgvector_chunk_384": 384,
}


def _load_entries(directory: Path, schema):
    entries = []
    for path in sorted(directory.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            entries.append(schema.model_validate(payload))
        except Exception as exc:
            raise ValueError(f"Invalid embedding catalog file: {path}") from exc
    return entries


@lru_cache(maxsize=1)
def load_embedding_catalog() -> list[EmbeddingCatalogEntry]:
    models = _load_entries(MODELS_DIR, EmbeddingModelEntry)
    profiles = _load_entries(PROFILES_DIR, EmbeddingProfileEntry)

    model_by_id: dict[str, EmbeddingModelEntry] = {}
    for model in models:
        if model.id in model_by_id:
            raise ValueError(f"Duplicate embedding model id: {model.id}")
        model_by_id[model.id] = model

    presets_by_entry_id: dict[str, list[str]] = {}
    for preset, entry_id in EMBEDDING_PRESETS.items():
        presets_by_entry_id.setdefault(entry_id, []).append(preset)

    resolved = []
    profile_ids = set()
    for profile in profiles:
        if profile.id in profile_ids:
            raise ValueError(f"Duplicate embedding profile id: {profile.id}")
        profile_ids.add(profile.id)
        model = model_by_id.get(profile.model_id)
        if model is None:
            raise ValueError(
                f"Embedding profile {profile.id} references unknown model "
                f"{profile.model_id}."
            )
        resolved.append(resolve_embedding_profile(
            model, profile, presets=presets_by_entry_id.get(profile.id, [])
        ))

    resolved_by_id = {entry.id: entry for entry in resolved}
    for preset, entry_id in EMBEDDING_PRESETS.items():
        target = resolved_by_id.get(entry_id)
        if target is None:
            raise ValueError(
                f"Embedding preset {preset} points to unknown catalog entry {entry_id}."
            )
        if target.availability != "supported":
            raise ValueError(
                f"Embedding preset {preset} points to non-supported catalog entry {entry_id}."
            )

    return sorted(resolved, key=lambda item: item.priority, reverse=True)


def resolve_embedding_profile(
    model: EmbeddingModelEntry, profile: EmbeddingProfileEntry, *, presets=None,
) -> EmbeddingCatalogEntry:
    """Resolve a candidate pair using the same checks as bundled profiles."""
    if profile.model_id != model.id:
        raise ValueError("Embedding profile references a different model")
    if profile.dimension != model.dimension and not (
        profile.provider == "sentence_transformers"
        and profile.provider_options.get("truncate_dim") == profile.dimension
        and profile.dimension < model.dimension
    ) and not (
        profile.provider == "openai_compatible"
        and profile.provider_options.get("send_dimensions") is True
        and profile.dimension < model.dimension
    ):
        raise ValueError(
            f"Embedding profile {profile.id} dimension does not match model."
        )
    try:
        validate_function_list(profile.functions)
    except ValueError as exc:
        raise ValueError(f"Invalid functions for embedding profile {profile.id}.") from exc
    for function in profile.functions:
        if function.function_id != "text.embed":
            raise ValueError(
                f"Embedding profile {profile.id} cannot declare {function.function_id}."
            )
        if function.backend_ids and profile.provider not in function.backend_ids:
            raise ValueError(
                f"Function {function.function_id} is incompatible with provider {profile.provider}."
            )
        if function.verification != "declared_unverified":
            try:
                validate_function_evidence_target(
                    function,
                    model_id=model.id,
                    model_revision=model.revision,
                    target_id=profile.id,
                    target_revision=model.revision,
                    artifact_digest=model.revision,
                    backend_id=profile.provider,
                )
            except ValueError as exc:
                raise ValueError(
                    f"Invalid evidence for function {function.function_id} "
                    f"on embedding profile {profile.id}."
                ) from exc
    if profile.availability == "supported":
        if profile.provider not in KNOWN_PROVIDERS:
            raise ValueError(
                f"Supported profile {profile.id} references unknown provider "
                f"{profile.provider}."
            )
        store_dimension = KNOWN_STORES.get(profile.store)
        if store_dimension != profile.dimension:
            raise ValueError(
                f"Supported profile {profile.id} is incompatible with store "
                f"{profile.store}."
            )

    return EmbeddingCatalogEntry(
        id=profile.id,
        display_name=model.display_name,
        description=model.description,
        license=model.license,
        model_id=model.id,
        repo_id=model.repo_id,
        revision=model.revision,
        tokenizer_id=model.tokenizer_id,
        tokenizer_revision=model.tokenizer_revision,
        tokenizer_options=model.tokenizer_options,
        provider=profile.provider,
        store=profile.store,
        dimension=profile.dimension,
        model_input_max_tokens=model.model_input_max_tokens,
        supports_sparse=profile.supports_sparse,
        normalize_embeddings=profile.normalize_embeddings,
        distance_strategy=profile.distance_strategy,
        query_prefix=profile.query_prefix,
        document_prefix=profile.document_prefix,
        availability=profile.availability,
        priority=profile.priority,
        presets=presets or [],
        languages=model.languages,
        footprint=model.footprint,
        functions=profile.functions,
        provider_options=profile.provider_options,
    )


def get_supported_embedding_catalog() -> list[EmbeddingCatalogEntry]:
    return [
        entry
        for entry in load_embedding_catalog()
        if entry.availability == "supported"
    ]


def get_embedding_catalog_entry(entry_id: str) -> EmbeddingCatalogEntry | None:
    normalized = (entry_id or "").strip()
    return next(
        (entry for entry in load_embedding_catalog() if entry.id == normalized),
        None,
    )


def get_embedding_catalog_entry_for_preset(
    preset: str,
) -> EmbeddingCatalogEntry:
    normalized = (preset or "balanced").strip().lower()
    entry_id = EMBEDDING_PRESETS.get(normalized)
    entry = get_embedding_catalog_entry(entry_id) if entry_id else None
    if entry is None or entry.availability != "supported":
        raise ValueError(
            f"No supported embedding catalog entry for preset: {normalized}"
        )
    return entry
