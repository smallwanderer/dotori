import json
from functools import lru_cache
from pathlib import Path

from llm_installation.catalog.models import (
    ArtifactCatalogEntry,
    ModelCatalogEntry,
    RAGModelCatalogDocument,
    RAGModelCatalogEntry,
)
from llm_installation.function_catalog import validate_function_evidence_target

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
    artifact_by_id = {artifact.id: artifact for artifact in artifacts}
    known_backends = {"vllm-cuda", "llamacpp-cpu", "llamacpp-gpu-offload"}
    for artifact in artifacts:
        if artifact.id in artifact_ids:
            raise ValueError(f"Duplicate artifact id: {artifact.id}")
        artifact_ids.add(artifact.id)
        model = model_by_id.get(artifact.model_id)
        if model is None:
            raise ValueError(
                f"Artifact {artifact.id} references unknown model {artifact.model_id}."
            )
        for function in model.functions:
            missing_artifacts = set(function.artifact_ids) - set(artifact_by_id)
            if missing_artifacts:
                raise ValueError(
                    f"Function {function.function_id} references unknown artifacts: "
                    f"{', '.join(sorted(missing_artifacts))}."
                )
            if set(function.backend_ids) - known_backends:
                raise ValueError(
                    f"Function {function.function_id} references an unknown backend."
                )
            wrong_model_artifacts = {
                artifact_id for artifact_id in function.artifact_ids
                if artifact_by_id[artifact_id].model_id != model.id
            }
            if wrong_model_artifacts:
                raise ValueError(
                    f"Function {function.function_id} references artifacts from another model."
                )
        applicable_functions = []
        for function in model.functions:
            if function.artifact_ids and artifact.id not in function.artifact_ids:
                continue
            if function.backend_ids and not set(function.backend_ids).intersection(artifact.backend_profiles):
                if artifact.id in function.artifact_ids:
                    raise ValueError(
                        f"Function {function.function_id} is incompatible with artifact {artifact.id}."
                    )
                continue
            if set(function.backend_ids) - known_backends:
                raise ValueError(
                    f"Function {function.function_id} references an unknown backend."
                )
            applicable_functions.append(function)
            if function.verification != "declared_unverified":
                if not artifact.artifact.sha256:
                    raise ValueError(
                        f"Verified function {function.function_id} requires a SHA-256 "
                        f"digest on artifact {artifact.id}."
                    )
                applicable_backends = (
                    set(function.backend_ids)
                    if function.backend_ids
                    else set(artifact.backend_profiles)
                ) & set(artifact.backend_profiles)
                for backend_id in applicable_backends:
                    try:
                        validate_function_evidence_target(
                            function,
                            model_id=model.id,
                            model_revision=artifact.hf.revision,
                            target_id=artifact.id,
                            target_revision=artifact.hf.revision,
                            artifact_digest=artifact.artifact.sha256,
                            backend_id=backend_id,
                        )
                    except ValueError as exc:
                        raise ValueError(
                            f"Invalid evidence for function {function.function_id} "
                            f"on artifact {artifact.id}/{backend_id}."
                        ) from exc
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
                functions=applicable_functions,
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
