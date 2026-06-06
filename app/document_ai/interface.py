from dataclasses import dataclass
from typing import Protocol


@dataclass
class EmbeddingResult:
    dense_vector: list[float] | None = None
    sparse_vector: dict[str, float] | None = None
    model_name: str
    model_version: str
    demension: int | None
    metadata: dict


class EmbeddingProvider(Protocol):
    priovider_name: str

    def embed_query(self, query: str) -> EmbeddingResult:
        ... 
    
    def embed_chunks(self, chunks: list[DocumentChunk]) -> list[EmbeddingResult]:
        ... 


