import json
from functools import lru_cache
from pathlib import Path

from document_ai.llm_installation_helper.catalog.models import (
    ArtifactCatalogEntry,
    ModelCatalogEntry,
    RAGModelCatalogDocument,
    RAGModelCatalogEntry,
)

CATALOG_DIR = Path(__file__).parent
DEFAULTS_PATH = CATALOG_DIR / "defaults.json"
MODELS_DIR = CATALOG_DIR / "models"
ARTIFACTS_DIR = CATALOG_DIR / "artifacts"


def _load_entries(directory: Path, schema):
    entries = []
    if not directory.is_dir():
        return entries
    for json_file in sorted(directory.rglob("*.json")):
        try:
            with json_file.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            entries.append(schema.model_validate(payload))
        except Exception as exc:
            raise ValueError(f"Invalid catalog file: {json_file}") from exc
    return entries


def _resolve_candidates(models, artifacts) -> list[RAGModelCatalogEntry]:
    model_by_id = {}
    for model in models:
        if model.id in model_by_id:
            raise ValueError(f"Duplicate model id: {model.id}")
        model_by_id[model.id] = model

    artifact_ids = set()
    candidates = []
    for artifact in artifacts:
        if artifact.id in artifact_ids:
            raise ValueError(f"Duplicate artifact id: {artifact.id}")
        artifact_ids.add(artifact.id)
        model = model_by_id.get(artifact.model_id)
        if model is None:
            raise ValueError(
                f"Artifact {artifact.id} references unknown model {artifact.model_id}."
            )
        candidates.append(
            RAGModelCatalogEntry(
                id=artifact.id,
                model_id=model.id,
                enabled=artifact.enabled,
                priority=artifact.priority,
                display_name=artifact.display_name or model.display_name,
                description=artifact.description or model.description,
                license=model.license,
                hf=artifact.hf,
                artifact=artifact.artifact,
                backend_profiles=artifact.backend_profiles,
                model_metadata=model.model_metadata,
                capabilities=model.capabilities,
                policy=artifact.policy,
            )
        )
    return candidates


def _load_catalog_document() -> RAGModelCatalogDocument:
    schema_version = 2
    catalog_name = "rag-serving-model-catalog"
    updated_at = None

    if DEFAULTS_PATH.exists():
        try:
            with DEFAULTS_PATH.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict):
                schema_version = payload.get("schema_version", 2)
                catalog_name = payload.get("catalog_name", "rag-serving-model-catalog")
                updated_at = payload.get("updated_at")
        except Exception:
            pass

    models = _load_entries(MODELS_DIR, ModelCatalogEntry)
    artifacts = _load_entries(ARTIFACTS_DIR, ArtifactCatalogEntry)
    entries = _resolve_candidates(models, artifacts)

    return RAGModelCatalogDocument(
        schema_version=schema_version,
        catalog_name=catalog_name,
        updated_at=updated_at,
        entries=entries,
    )


@lru_cache(maxsize=1)
def get_rag_model_catalog() -> list[RAGModelCatalogEntry]:
    document = _load_catalog_document()

    entries = [
        entry
        for entry in document.entries
        if entry.id.strip()
    ]

    return sorted(entries, key=lambda item: item.priority, reverse=True)


def get_catalog_entry(model_id: str) -> RAGModelCatalogEntry | None:
    normalized = (model_id or "").strip()

    for entry in get_rag_model_catalog():
        if entry.id == normalized:
            return entry

    return None


def search_rag_model_catalog(query: str) -> list[RAGModelCatalogEntry]:
    normalized = (query or "").strip().lower()

    if not normalized:
        return get_rag_model_catalog()

    return [
        entry
        for entry in get_rag_model_catalog()
        if normalized in entry.id.lower()
        or normalized in entry.display_name.lower()
        or normalized in entry.description.lower()
        or normalized in entry.hf.repo_id.lower()
        or normalized in entry.artifact.quant.lower()
        or normalized in entry.artifact.format.lower()
        or any(normalized in tag.lower() for tag in entry.policy.tags)
    ]
