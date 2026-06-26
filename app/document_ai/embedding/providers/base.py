from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmbeddingProviderSpec:
    backend: str
    model_name: str
    dimension: int | None
    supports_sparse: bool
    supports_dense: bool = True
    default_distance: str = "inner_product"


@dataclass
class EmbeddingResult:
    dense_vector: list[float]
    sparse_vector: dict[str, float]
    model_name: str = ""
    backend: str = ""
    dimension: int | None = None


class EmbeddingProvider(Protocol):
    spec: EmbeddingProviderSpec

    def embed_document(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        ...

    def embed_query(self, text: str, *, max_length: int | None = None) -> EmbeddingResult:
        ...

    def healthcheck(self) -> dict:
        ...
