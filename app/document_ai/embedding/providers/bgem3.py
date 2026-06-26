from __future__ import annotations

import gc
import logging
import math
from typing import Any, Dict

from document_ai.parsers.config import get_embedding_max_tokens

from .base import EmbeddingProviderSpec, EmbeddingResult

try:
    import torch
except ImportError:  # pragma: no cover - exercised only when torch is unavailable
    torch = None

logger = logging.getLogger(__name__)

_MODEL_CACHE: Dict[str, Any] = {}
if torch is not None:
    _DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")
else:
    _DEVICE = None


def clear_cuda_cache() -> None:
    if torch is not None and _DEVICE is not None and _DEVICE.type == "cuda":
        torch.cuda.empty_cache()
        if torch.cuda.is_available():
            torch.cuda.ipc_collect()


def get_bgem3_model(model_name: str):
    model = _MODEL_CACHE.get(model_name)
    if model is None:
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:
            raise RuntimeError(
                "FlagEmbedding is required for bgem3_hybrid embeddings."
            ) from exc

        model = BGEM3FlagModel(
            model_name,
            use_fp16=(_DEVICE is not None and _DEVICE.type == "cuda"),
            normalize_embeddings=True,
        )
        _MODEL_CACHE[model_name] = model
    return model


def validate_text(text: str) -> str:
    if not isinstance(text, str):
        raise ValueError("Text not String")

    normalized = text.strip()
    if not normalized:
        raise ValueError("Text is Empty")

    return normalized


def normalize_sparse_vector(weights: dict[str, float]) -> dict[str, float]:
    if not weights:
        return {}

    norm = math.sqrt(sum(value * value for value in weights.values()))
    if norm <= 0:
        return {}

    return {
        key: value / norm
        for key, value in weights.items()
        if value > 0
    }


def check_normalized(vector: list[float]) -> list[float]:
    if not vector:
        return []

    norm = math.sqrt(sum(v * v for v in vector))
    if norm <= 0:
        return vector

    if not math.isclose(norm, 1.0, rel_tol=1e-5):
        return [v / norm for v in vector]

    return vector


def coerce_sparse_vector(raw_sparse: dict) -> dict[str, float]:
    sparse_vector = {
        str(key): float(value)
        for key, value in (raw_sparse or {}).items()
        if float(value) > 0
    }
    return normalize_sparse_vector(sparse_vector)


def coerce_dense_vector(raw_dense) -> list[float]:
    if hasattr(raw_dense, "tolist"):
        raw_dense = raw_dense.tolist()

    if raw_dense and isinstance(raw_dense[0], list):
        raw_dense = raw_dense[0]

    vector = [float(value) for value in raw_dense]
    if not vector:
        raise RuntimeError("Embedding vector is empty")
    return check_normalized(vector)


class BGEM3HybridProvider:
    backend = "bgem3_hybrid"

    def __init__(self, *, model_name: str, dimension: int | None = None):
        self.spec = EmbeddingProviderSpec(
            backend=self.backend,
            model_name=model_name,
            dimension=dimension,
            supports_sparse=True,
            default_distance="inner_product",
        )

    def embed_document(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        return self._embed(text, max_length=max_length)

    def embed_query(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        return self._embed(text, max_length=max_length)

    def healthcheck(self) -> dict:
        result = self.embed_query("embedding healthcheck", max_length=32)
        return {
            "backend": result.backend,
            "model_name": result.model_name,
            "dimension": result.dimension,
            "supports_sparse": bool(result.sparse_vector),
        }

    def _embed(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        normalized_text = validate_text(text)
        resolved_max_length = max_length or get_embedding_max_tokens()
        model = get_bgem3_model(self.spec.model_name)

        try:
            output = model.encode(
                [normalized_text],
                batch_size=1,
                max_length=resolved_max_length,
                return_dense=True,
                return_sparse=True,
                return_colbert_vecs=False,
            )

            dense_vector = coerce_dense_vector(output["dense_vecs"])
            lexical_weights = output.get("lexical_weights") or [{}]
            if isinstance(lexical_weights, list):
                lexical_weights = lexical_weights[0] if lexical_weights else {}
            sparse_vector = coerce_sparse_vector(lexical_weights)

            return EmbeddingResult(
                dense_vector=dense_vector,
                sparse_vector=sparse_vector,
                model_name=self.spec.model_name,
                backend=self.spec.backend,
                dimension=len(dense_vector),
            )

        except Exception as exc:
            if torch is not None and isinstance(exc, torch.cuda.OutOfMemoryError):
                logger.exception(
                    "CUDA OOM during embedding. model=%s, backend=%s, max_length=%s, device=%s",
                    self.spec.model_name,
                    self.spec.backend,
                    resolved_max_length,
                    _DEVICE,
                )
                clear_cuda_cache()
                gc.collect()
                raise RuntimeError(
                    "GPU OOM while embedding text "
                    f"(model={self.spec.model_name}, backend={self.spec.backend}, max_length={resolved_max_length})"
                ) from exc
            raise
        finally:
            gc.collect()
            if torch is not None and _DEVICE is not None and _DEVICE.type == "cuda":
                torch.cuda.empty_cache()
