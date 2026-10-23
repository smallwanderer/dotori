from __future__ import annotations

import os

from capabilities.providers import validate_embedding_registration

from .providers import (
    BGEM3HybridProvider,
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
    SentenceTransformersEmbeddingProvider,
)
from .providers.remote import RemoteEmbeddingProxyProvider
from documents.services.embedding_runtime_config import (
    EmbeddingRuntimeSnapshot,
    get_active_embedding_runtime,
)

_PROVIDER_FACTORIES = {
    BGEM3HybridProvider.backend: BGEM3HybridProvider,
    SentenceTransformersEmbeddingProvider.backend: SentenceTransformersEmbeddingProvider,
    OpenAIEmbeddingProvider.backend: OpenAIEmbeddingProvider,
}

validate_embedding_registration(_PROVIDER_FACTORIES)


def _is_embedding_model_process() -> bool:
    # Only the gunicorn worker embedding-executor boots as its model owner sets
    # this (see docker-compose.yml). app and dotori-orchestrator do not set it
    # and proxy through the same /embed endpoint, so the model is loaded in
    # exactly one OS process.
    return os.getenv("DOTORI_EMBEDDING_MODEL_PROCESS") == "1"


def _resolve_embedding_adapter(
    *,
    remote: bool,
    backend: str | None = None,
    model_name: str | None = None,
    dimension: int | None = None,
    runtime: EmbeddingRuntimeSnapshot | None = None,
) -> EmbeddingProvider:
    resolved_runtime = runtime or get_active_embedding_runtime()
    resolved_backend = backend or resolved_runtime.provider
    resolved_model = model_name or resolved_runtime.model_id
    resolved_dimension = (
        dimension if dimension is not None else resolved_runtime.dimension
    )

    try:
        factory = _PROVIDER_FACTORIES[resolved_backend]
    except KeyError as exc:
        raise ValueError(f"Unsupported embedding backend: {resolved_backend}") from exc

    # Only the model-owning executor may construct a provider. This applies to
    # external providers too: query and document calls then share the same
    # admission queue, timeout policy, and server-wide runtime contract.
    model_revision = (
        getattr(resolved_runtime, "model_revision", "")
        if resolved_model == getattr(resolved_runtime, "model_id", None)
        else ""
    )
    normalize_embeddings = getattr(resolved_runtime, "normalize_embeddings", True)
    query_prefix = getattr(resolved_runtime, "query_prefix", "")
    document_prefix = getattr(resolved_runtime, "document_prefix", "")

    if remote:
        supports_sparse = getattr(
            resolved_runtime,
            "supports_sparse",
            resolved_backend == BGEM3HybridProvider.backend,
        )
        default_distance = getattr(resolved_runtime, "distance_strategy", "cosine")
        return RemoteEmbeddingProxyProvider(
            backend=resolved_backend,
            model_name=resolved_model,
            model_revision=model_revision,
            dimension=resolved_dimension,
            supports_sparse=supports_sparse,
            normalize_embeddings=normalize_embeddings,
            query_prefix=query_prefix,
            document_prefix=document_prefix,
            default_distance=default_distance,
            runtime_fingerprint=resolved_runtime.runtime_fingerprint,
        )

    options = getattr(resolved_runtime, "provider_options", {})
    # Legacy callers may override the backend/model; do not leak another
    # model's adapter options into that compatibility path.
    if resolved_backend != resolved_runtime.provider or resolved_model != resolved_runtime.model_id:
        options = {}
    factory_kwargs = {}
    if resolved_backend in {"sentence_transformers", "openai_compatible"}:
        factory_kwargs["provider_options"] = options
    if resolved_backend == "sentence_transformers":
        factory_kwargs["max_tokens"] = resolved_runtime.max_tokens
    return factory(
        model_name=resolved_model,
        model_revision=model_revision,
        dimension=resolved_dimension,
        normalize_embeddings=normalize_embeddings,
        query_prefix=query_prefix,
        document_prefix=document_prefix,
        default_distance=resolved_runtime.distance_strategy,
        **factory_kwargs,
    )


def get_embedding_client(**kwargs) -> EmbeddingProvider:
    """Request-process client; never constructs a model implementation."""
    return _resolve_embedding_adapter(remote=True, **kwargs)


def create_embedding_implementation(**kwargs) -> EmbeddingProvider:
    """Construct an implementation only in the model-owning executor."""
    if not _is_embedding_model_process():
        raise RuntimeError("Embedding implementations belong to the model-owning executor")
    return _resolve_embedding_adapter(remote=False, **kwargs)


def get_embedding_provider(**kwargs) -> EmbeddingProvider:
    """Compatibility facade for callers predating explicit client/implementation APIs."""
    factory = create_embedding_implementation if _is_embedding_model_process() else get_embedding_client
    return factory(**kwargs)


def registered_embedding_backends() -> list[str]:
    return sorted(_PROVIDER_FACTORIES)
