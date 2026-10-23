"""Read-only candidate resolution shared by the two executor diagnostics."""
from llm_installation.embedding_catalog.loader import get_embedding_catalog_entry, resolve_embedding_profile
from llm_installation.embedding_catalog.models import EmbeddingModelEntry, EmbeddingProfileEntry
from llm_installation.embedding_config_store import build_embedding_runtime_payload
from documents.services.embedding_runtime_config import EmbeddingRuntimeSnapshot


def resolve_candidate(options):
    if options.get("catalog_id"):
        if options.get("profile_file"):
            raise ValueError("--profile-file requires --model-file")
        entry = get_embedding_catalog_entry(options["catalog_id"])
        if entry is None:
            raise ValueError("Unknown embedding catalog ID")
    else:
        if not options.get("profile_file"):
            raise ValueError("--model-file requires --profile-file")
        model = EmbeddingModelEntry.model_validate_json(options["model_file"].read_text(encoding="utf-8"))
        profile = EmbeddingProfileEntry.model_validate_json(options["profile_file"].read_text(encoding="utf-8"))
        entry = resolve_embedding_profile(model, profile)
    if entry.availability == "unavailable":
        raise ValueError("Cannot validate an unavailable profile")
    payload = build_embedding_runtime_payload(scope="candidate", generation_id="candidate-validation", entry=entry)
    payload["functions"] = []
    return EmbeddingRuntimeSnapshot.model_validate(payload)


def candidate_contract(runtime):
    return runtime.model_dump(mode="json", exclude={
        "resolved_at", "runtime_fingerprint", "generation_id", "scope", "functions",
    })
