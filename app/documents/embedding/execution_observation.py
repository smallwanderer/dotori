"""Execution-owner observations; do not infer backend versions from catalog evidence."""
from importlib.metadata import PackageNotFoundError, version

from llm_installation.embedding_catalog.loader import get_embedding_catalog_entry
from llm_installation.function_applicability import ObservedFunctionExecution


def observe_embedding_execution(runtime, provider):
    if runtime.provider != "bgem3_hybrid":
        return None  # External endpoints and other adapters need their own observer.
    from .providers import bgem3
    if bgem3._DEVICE is None:
        return None  # Never substitute detected hardware for the loaded model's device.
    if (provider.spec.model_name != runtime.model_id
            or provider.spec.model_revision != runtime.model_revision
            or any(getattr(provider.spec, key) != getattr(runtime, runtime_key) for key, runtime_key in (
                ("backend", "provider"), ("dimension", "dimension"),
                ("normalize_embeddings", "normalize_embeddings"), ("query_prefix", "query_prefix"),
                ("document_prefix", "document_prefix"), ("default_distance", "distance_strategy")))):
        return None
    entry = get_embedding_catalog_entry(runtime.catalog_id)
    if entry.repo_id != runtime.model_id or entry.revision != runtime.model_revision:
        return None
    try:
        backend_version = "_".join([
            f"FlagEmbedding-{version('FlagEmbedding')}", f"torch-{version('torch')}",
            f"transformers-{version('transformers')}", str(bgem3._DEVICE.type)])
    except PackageNotFoundError:
        return None
    return ObservedFunctionExecution(
        observation_ref=f"embedding-executor:{runtime.generation_id}",
        runtime_generation_id=runtime.generation_id, model_id=entry.model_id,
        model_revision=provider.spec.model_revision, target_id=runtime.catalog_id,
        target_revision=runtime.model_revision, artifact_digest=runtime.model_revision,
        backend_id=provider.spec.backend, backend_version=backend_version,
        adapter_contract="dotori-internal-embed-v1", contract_version="text-embed-v1",
        input_preprocessing_version="dotori-prefix-token-limit-v1",
        output_parsing_version="dotori-dense-sparse-v1",
        inference_options={key: getattr(runtime, key) for key in (
            "dimension", "distance_strategy", "document_prefix", "max_tokens",
            "normalize_embeddings", "query_prefix", "store")})
