"""Embedding transport identity; no model loading or catalog evaluation at request time."""
from capabilities.deployment import load_deployment_snapshot
from documents.services.embedding_runtime_config import get_active_embedding_runtime


def runtime_identity(runtime):
    return {
        "model_installation_id": f"embedding:{runtime.generation_id}",
        "runtime_generation_id": runtime.generation_id,
        "runtime_fingerprint": runtime.runtime_fingerprint,
        "catalog_id": runtime.catalog_id,
        "catalog_revision": runtime.catalog_revision,
        "model_name": runtime.model_id,
        "model_revision": runtime.model_revision,
        "backend": runtime.provider,
        "dimension": runtime.dimension,
    }


def resolve_embedding_identity(spec, bound_fingerprint):
    try:
        runtime = get_active_embedding_runtime()
        snapshot = load_deployment_snapshot()
        identity = runtime_identity(runtime)
        if bound_fingerprint != runtime.runtime_fingerprint:
            raise ValueError("Cached embedding proxy differs from active runtime")
        for field, expected in (("model_name", runtime.model_id),
                                ("model_revision", runtime.model_revision),
                                ("backend", runtime.provider), ("dimension", runtime.dimension),
                                ("normalize_embeddings", runtime.normalize_embeddings),
                                ("query_prefix", runtime.query_prefix),
                                ("document_prefix", runtime.document_prefix),
                                ("default_distance", runtime.distance_strategy),
                                ("supports_sparse", runtime.supports_sparse)):
            if getattr(spec, field) != expected:
                raise ValueError("Embedding proxy specification differs from active runtime")
        if not snapshot.provider_enabled("embedding"):
            raise ValueError("Embedding role is disabled")
        # Schema 2 retains its explicit role-based compatibility path.
        if snapshot.schema_version >= 3:
            candidate = snapshot.resolve_function_provider("text.embed", preferred_role="embedding")
            installation_id = identity["model_installation_id"]
            model = snapshot.installed_models.get(installation_id, {})
            binding = snapshot.role_bindings.get("embedding", {})
            if (candidate.get("role") != "embedding"
                    or candidate.get("model_installation_id") != installation_id
                    or binding.get("model_installation_id") != installation_id
                    or model.get("catalog_id") != runtime.catalog_id
                    or model.get("artifact_revision") != (runtime.model_revision or runtime.runtime_fingerprint or runtime.generation_id)
                    or candidate.get("adapter_contract") != "dotori-internal-embed-v1"):
                raise ValueError("text.embed route differs from active runtime")
        return identity
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"No usable deployment route for text.embed: {exc}") from exc
