from __future__ import annotations

import gc
import logging
import threading
from typing import Any, Dict

from llm_installation.embedding_catalog.provider_options import SentenceTransformersOptions

from documents.parsers.config import get_embedding_max_tokens

from .base import EmbeddingProviderSpec, EmbeddingResult
from .bgem3 import (
    clear_cuda_cache as _clear_cuda_cache,
    coerce_dense_vector as _coerce_dense_vector,
    validate_text as _validate_text,
)

logger = logging.getLogger(__name__)

_ST_MODEL_CACHE: Dict[str, Any] = {}
_MODEL_OPERATION_LOCK = threading.RLock()
_torch_module = None
_torch_import_attempted = False
_DEVICE = None


def _ensure_torch():
    global _torch_module, _torch_import_attempted, _DEVICE
    if _torch_import_attempted:
        return _torch_module
    _torch_import_attempted = True
    try:
        import torch as torch_module
    except ImportError:
        _torch_module = None
    else:
        _torch_module = torch_module
        _DEVICE = torch_module.device("cuda" if torch_module.cuda.is_available() else "cpu")
    return _torch_module


_MODEL_LOAD_LOCK = threading.Lock()


def get_sentence_transformer_model(model_name: str, model_revision: str = "", *, trust_remote_code: bool = False):
    with _MODEL_LOAD_LOCK:
        return _load_model(model_name, model_revision, trust_remote_code=trust_remote_code)


def _load_model(model_name: str, model_revision: str = "", *, trust_remote_code: bool = False):
    cache_key = f"{model_name}@{model_revision or 'default'}"
    if trust_remote_code:
        cache_key += ":trusted-code"
    model = _ST_MODEL_CACHE.get(cache_key)
    if model is None:
        _ensure_torch()
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is required for sentence_transformers embeddings."
            ) from exc

        kwargs: dict[str, Any] = {}
        if model_revision:
            kwargs["revision"] = model_revision

        if trust_remote_code:
            kwargs["trust_remote_code"] = True

        logger.info(
            "Loading sentence_transformers model: %s (revision=%s)",
            model_name,
            model_revision or "default",
        )
        model = SentenceTransformer(model_name, **kwargs)
        if _DEVICE is not None:
            model = model.to(_DEVICE)
        _ST_MODEL_CACHE[cache_key] = model
    return model


class SentenceTransformersEmbeddingProvider:
    backend = "sentence_transformers"

    def __init__(
        self,
        *,
        model_name: str,
        model_revision: str = "",
        dimension: int | None = None,
        normalize_embeddings: bool = True,
        query_prefix: str = "",
        document_prefix: str = "",
        default_distance: str = "inner_product",
        provider_options: dict | None = None,
        max_tokens: int | None = None,
    ):
        self.options = SentenceTransformersOptions.model_validate(provider_options or {})
        self.max_tokens = max_tokens
        self.spec = EmbeddingProviderSpec(
            backend=self.backend,
            model_name=model_name,
            model_revision=model_revision,
            dimension=dimension,
            supports_sparse=False,
            supports_dense=True,
            default_distance=default_distance,
            normalize_embeddings=normalize_embeddings,
            query_prefix=query_prefix,
            document_prefix=document_prefix,
        )

    def embed_document(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        normalized_text = _validate_text(text)
        if self.spec.document_prefix:
            normalized_text = f"{self.spec.document_prefix}{normalized_text}"
        return self._embed_single(normalized_text, max_length=max_length)

    def embed_query(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        normalized_text = _validate_text(text)
        if self.spec.query_prefix:
            normalized_text = f"{self.spec.query_prefix}{normalized_text}"
        return self._embed_single(normalized_text, max_length=max_length, kind="query")

    def embed_documents(self, texts: list[str], *, max_length: int | None = None) -> list[EmbeddingResult]:
        if not texts:
            return []
        prepared_texts = []
        for t in texts:
            normalized = _validate_text(t)
            if self.spec.document_prefix:
                normalized = f"{self.spec.document_prefix}{normalized}"
            prepared_texts.append(normalized)
        return self._embed_batch(prepared_texts, max_length=max_length)

    def healthcheck(self) -> dict:
        result = self.embed_query("healthcheck")
        dim = len(result.dense_vector)
        return {
            "status": "ready",
            "backend": self.backend,
            "model_name": self.spec.model_name,
            "dimension": dim,
            "supports_sparse": False,
        }

    def _get_model(self):
        load_options = {"trust_remote_code": True} if self.options.trust_remote_code else {}
        return get_sentence_transformer_model(
            self.spec.model_name, self.spec.model_revision, **load_options,
        )

    def _encode_options(self, kind: str) -> dict:
        options = {}
        for field in ("task", "prompt_name"):
            value = getattr(self.options, f"{kind}_{field}")
            if value is not None:
                options[field] = value
        if getattr(self.spec, f"{kind}_prefix"):
            # The profile already supplied a complete prompt.
            options["prompt"] = ""
        if self.options.truncate_dim is not None:
            options["truncate_dim"] = self.options.truncate_dim
        return options

    def _embed_single(self, text: str, *, max_length: int | None = None, kind: str = "document") -> EmbeddingResult:
        results = self._embed_batch([text], max_length=max_length, kind=kind)
        if not results:
            raise RuntimeError("SentenceTransformer produced no embeddings.")
        return results[0]

    def _embed_batch(self, texts: list[str], *, max_length: int | None = None, kind: str = "document") -> list[EmbeddingResult]:
        model = self._get_model()
        encode_options = self._encode_options(kind)
        torch = _ensure_torch()

        # A cached model has mutable tokenizer limits. Serialize and restore the
        # setting so query caps cannot leak into document calls.
        _MODEL_OPERATION_LOCK.acquire()
        original_limit = getattr(model, "max_seq_length", None)
        try:
            limit = max_length or self.max_tokens or get_embedding_max_tokens()
            if original_limit is not None:
                model.max_seq_length = min(limit, original_limit)
                limit = min(limit, original_limit)
            tokenizer = getattr(model, "tokenizer", None)
            if tokenizer is not None:
                from documents.embedding.input_budget import fit_embedding_text, sentence_transformer_prompt
                prompt = sentence_transformer_prompt(model, encode_options)
                texts = [fit_embedding_text(text, tokenizer, max_tokens=limit,
                    prompt=prompt, required_prefix=getattr(self.spec, f"{kind}_prefix"))[0] for text in texts]
            embeddings = model.encode(
                texts,
                batch_size=len(texts),
                normalize_embeddings=self.spec.normalize_embeddings,
                show_progress_bar=False,
                **encode_options,
            )
            if hasattr(embeddings, "tolist"):
                embeddings = embeddings.tolist()

            results: list[EmbeddingResult] = []
            for row in embeddings:
                dense_vector = _coerce_dense_vector(row, normalize=self.spec.normalize_embeddings)
                results.append(
                    EmbeddingResult(
                        dense_vector=dense_vector,
                        sparse_vector={},
                        model_name=self.spec.model_name,
                        backend=self.spec.backend,
                        dimension=len(dense_vector),
                    )
                )
            return results

        except Exception as exc:
            if torch is not None and isinstance(exc, getattr(torch.cuda, "OutOfMemoryError", tuple())):
                logger.exception(
                    "CUDA OOM during sentence_transformers embedding. model=%s, batch_size=%s",
                    self.spec.model_name,
                    len(texts),
                )
                _clear_cuda_cache()
                gc.collect()
                raise RuntimeError(
                    f"GPU OOM while embedding with {self.spec.model_name}"
                ) from exc
            raise
        finally:
            if original_limit is not None:
                model.max_seq_length = original_limit
            _MODEL_OPERATION_LOCK.release()
            gc.collect()
            if torch is not None and _DEVICE is not None and getattr(_DEVICE, "type", "") == "cuda":
                _clear_cuda_cache()
