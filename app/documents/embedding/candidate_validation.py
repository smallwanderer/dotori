"""Synchronous diagnostics for a disposable embedding model process.

No active runtime lookup, publication, generation rows, or vector persistence.
"""
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import math
import re

from documents.embedding.registry import create_embedding_implementation
from documents.embedding.store_registry import get_embedding_store_instance


def _validate_result(result, runtime, store):
    store.validate_embedding(result)
    vector = result.dense_vector
    if result.dimension != runtime.dimension:
        raise ValueError("Output dimension metadata differs from the candidate")
    if result.backend != runtime.provider or result.model_name != runtime.model_id:
        raise ValueError("Output model/backend metadata differs from the candidate")
    if not vector or any(not math.isfinite(v) for v in vector):
        raise ValueError("Dense output must contain finite values")
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm or (runtime.normalize_embeddings and not math.isclose(norm, 1, abs_tol=1e-3)):
        raise ValueError("Dense output violates the normalization contract")
    if bool(result.sparse_vector) != runtime.supports_sparse:
        raise ValueError("Sparse output differs from supports_sparse")
    if any(not math.isfinite(v) for v in result.sparse_vector.values()):
        raise ValueError("Sparse output must contain finite values")


def _local_model(provider):
    if provider.backend == "sentence_transformers":
        return provider._get_model()
    from documents.embedding.providers.bgem3 import get_bgem3_model
    return get_bgem3_model(provider.spec.model_name, provider.spec.model_revision)


def _token_fixture(provider, runtime, model):
    """Compare an oversized input with its exact token-truncated equivalent."""
    tokenizer = model.tokenizer
    prefix = runtime.document_prefix
    if runtime.provider == "sentence_transformers" and not prefix:
        name = provider.options.document_prompt_name or getattr(model, "default_prompt_name", None)
        prefix = getattr(model, "prompts", {}).get(name, "") if name else ""
    short = "Dotori stores local documents for semantic search."
    ids = tokenizer(prefix + short, truncation=False)["input_ids"]
    cap = len(ids)
    model_cap = min(runtime.max_tokens, getattr(model, "max_seq_length", runtime.max_tokens))
    if cap > model_cap:
        raise ValueError("Token fixture prompt and sample exceed the candidate token cap")
    text = short + " Additional words extend this document well beyond the cap." * 100
    if len(tokenizer(prefix + text, truncation=False)["input_ids"]) <= cap:
        raise ValueError("Token fixture is not oversized")
    if tokenizer(prefix + text, truncation=True, max_length=cap)["input_ids"] != ids:
        raise ValueError("Token fixture cannot match the exact truncated input; token limit remains unverified")
    long_result = provider.embed_document(text, max_length=cap)
    short_result = provider.embed_document(short, max_length=cap)
    if not all(math.isclose(a, b, abs_tol=2e-3, rel_tol=2e-3)
               for a, b in zip(long_result.dense_vector, short_result.dense_vector)):
        raise ValueError("Oversized and token-truncated inputs produced different dense outputs")
    return cap, long_result, short_result


def validate_candidate(runtime, *, config_only=False, parser_report=None):
    report = {
        "schema_version": 1,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "candidate": runtime.model_dump(mode="json"),
        "mode": "config_only" if config_only else "execution",
        "checks": {}, "failures": [], "passed": False,
        "remaining_checks": ["retrieval_quality", "database_schema", "production_applicability", "parser_pipeline"],
        "observed_model_revision": None,
    }
    packages = {
        "sentence_transformers": ("sentence-transformers", "torch", "transformers"),
        "bgem3_hybrid": ("FlagEmbedding", "torch", "transformers"),
        "openai_compatible": ("httpx",),
    }
    report["library_versions"] = {}
    for package in packages.get(runtime.provider, ()):
        try:
            report["library_versions"][package] = version(package)
        except PackageNotFoundError:
            report["library_versions"][package] = None

    def check(name, operation):
        try:
            result = operation()
        except Exception as exc:
            report["checks"][name] = False
            report["failures"].append({"check": name, "error": str(exc)})
            return None
        report["checks"][name] = True
        return result

    def configuration():
        from capabilities.providers import EMBEDDING_BACKENDS
        from llm_installation.embedding_catalog.loader import KNOWN_STORES
        if runtime.provider not in EMBEDDING_BACKENDS:
            raise ValueError(f"Unsupported Provider: {runtime.provider}")
        if KNOWN_STORES.get(runtime.store) != runtime.dimension:
            raise ValueError("Candidate output dimension is incompatible with the selected store")
        if runtime.distance_strategy != "inner_product" or not runtime.normalize_embeddings:
            raise ValueError("Current Dotori search requires normalized inner_product embeddings")
        expected_sparse = runtime.provider == "bgem3_hybrid"
        if runtime.supports_sparse != expected_sparse:
            raise ValueError("supports_sparse differs from the selected Provider contract")
        return get_embedding_store_instance(runtime=runtime)

    store = check("configuration", configuration)
    if store is None:
        return report
    if config_only:
        report["passed"] = True
        report["remaining_checks"].extend(["model_execution", "token_limit", "loaded_artifact_identity"])
        return report

    provider = check("model_process", lambda: create_embedding_implementation(runtime=runtime))
    if provider is None:
        return report

    if parser_report is not None:
        def parser_contract():
            from documents.parsers.candidate_validation import parser_report_digest
            from documents.embedding.candidates import candidate_contract
            from documents.services.embedding_runtime_config import EmbeddingRuntimeSnapshot
            from documents.embedding.input_budget import fit_embedding_text, sentence_transformer_prompt
            if parser_report.get("kind") != "parser_candidate" or parser_report.get("passed") is not True:
                raise ValueError("Parser report must record a successful candidate validation")
            if parser_report.get("report_digest") != parser_report_digest(parser_report):
                raise ValueError("Parser report digest differs from its content")
            source_runtime = EmbeddingRuntimeSnapshot.model_validate(parser_report["candidate"])
            if candidate_contract(source_runtime) != candidate_contract(runtime):
                raise ValueError("Parser report and embedding candidate settings differ")
            identity = parser_report.get("tokenizer", {})
            if identity.get("fallback_used") is not False or identity.get("loaded_id") != runtime.tokenizer_id:
                raise ValueError("Parser report did not use the selected tokenizer")
            chunks = parser_report.get("chunks", [])
            if not chunks or len(chunks) > 4096:
                raise ValueError("Parser report must contain a bounded nonempty chunk list")
            if runtime.provider == "openai_compatible":
                from documents.parsers.tokenizers import load_runtime_tokenizer
                tokenizer = load_runtime_tokenizer(runtime, allow_fallback=False).tokenizer
                cap, prompt = runtime.max_tokens, ""
            else:
                model = _local_model(provider)
                tokenizer = model.tokenizer
                cap = min(runtime.max_tokens, getattr(model, "max_seq_length", runtime.max_tokens))
                if runtime.provider == "bgem3_hybrid":
                    cap = min(cap, 8192)
                prompt = sentence_transformer_prompt(model, provider._encode_options("document")) if runtime.provider == "sentence_transformers" else ""
            prepared, truncated = [], 0
            for chunk in chunks:
                text = chunk["text"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("Parser report contains empty or invalid chunk text")
                prefix = runtime.document_prefix
                bounded, count, changed = fit_embedding_text(prefix + text, tokenizer,
                    max_tokens=cap, prompt=prompt, required_prefix=prefix)
                prepared.append(bounded[len(prefix):] if prefix else bounded)
                truncated += int(changed)
            for offset in range(0, len(prepared), 4):
                inputs = prepared[offset:offset+4]
                outputs = provider.embed_documents(inputs, max_length=cap)
                if len(outputs) != len(inputs):
                    raise ValueError("Parser chunk embedding output count differs from inputs")
                for result in outputs:
                    _validate_result(result, runtime, store)
            report["parser_pipeline"] = {"chunk_count": len(prepared), "truncated_chunks": truncated,
                "effective_token_cap": cap, "report_digest": parser_report["report_digest"],
                "tokenizer": identity}
            report["remaining_checks"].remove("parser_pipeline")
            report["remaining_checks"].extend(k for k in parser_report.get("remaining_checks", [])
                if k not in report["remaining_checks"] and k != "embedding_execution")
            return True
        check("parser_pipeline", parser_contract)
        if not report["checks"]["parser_pipeline"]:
            return report

    text = "Dotori stores local documents for semantic search."

    def single():
        for operation in (provider.embed_query, provider.embed_document):
            _validate_result(operation(text, max_length=runtime.max_tokens), runtime, store)
        return True

    check("single_outputs", single)
    if not report["checks"]["single_outputs"]:
        report["remaining_checks"].extend(["batch_outputs", "token_limit", "loaded_artifact_identity"])
        return report

    def batch():
        texts = [text, "A second document discusses a different subject."]
        results = provider.embed_documents(texts, max_length=runtime.max_tokens)
        if len(results) != len(texts):
            raise ValueError("Batch output count differs from input count")
        for input_text, result in zip(texts, results):
            _validate_result(result, runtime, store)
            individual = provider.embed_document(input_text, max_length=runtime.max_tokens)
            _validate_result(individual, runtime, store)
            if not all(math.isclose(a, b, abs_tol=2e-3, rel_tol=2e-3)
                       for a, b in zip(result.dense_vector, individual.dense_vector)):
                raise ValueError("Batch order/output differs from individual document calls")
        return True

    check("batch_outputs", batch)
    if runtime.provider == "openai_compatible":
        report["remaining_checks"].extend(["external_model_identity", "external_model_revision", "external_token_limit"])
    elif report["checks"].get("single_outputs"):
        def tokens():
            model = _local_model(provider)
            cap, long_result, short_result = _token_fixture(provider, runtime, model)
            _validate_result(long_result, runtime, store)
            _validate_result(short_result, runtime, store)
            report["tested_token_cap"] = cap
            # HF tokenizer metadata can record the resolved commit. Do not
            # mistake the provider's requested revision for observed identity.
            tokenizer_revision = getattr(model.tokenizer, "init_kwargs", {}).get("_commit_hash")
            report["observed_tokenizer_revision"] = tokenizer_revision
            backbone = getattr(model, "model", None)
            if runtime.provider == "sentence_transformers":
                backbone = getattr(model[0], "auto_model", None)
            revision = getattr(getattr(backbone, "config", None), "_commit_hash", None)
            report["observed_model_revision"] = revision
            device = getattr(backbone, "device", None)
            report["observed_device"] = str(device) if device is not None else None
            if revision and re.fullmatch(r"[0-9a-f]{7,40}", runtime.model_revision) and not revision.startswith(runtime.model_revision):
                raise ValueError("Loaded model revision differs from requested model revision")
            return True
        check("token_limit", tokens)
        if not report["observed_model_revision"]:
            report["remaining_checks"].append("loaded_artifact_identity")
    else:
        report["remaining_checks"].extend(["token_limit", "loaded_artifact_identity"])
    report["passed"] = all(report["checks"].values())
    return report
