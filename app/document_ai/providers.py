


class BGEM3Provider:
    provider_name = "bge-m3"

    def __init__(self, model_name: str = "BAAI/bge-m3", backend: str = "hybrid"):
        self.model_name = model_name
        self.backend = backend

    def embed_query(self, query: str) -> EmbeddingResult:
        ... 
    
    def embed_chunks(self, chunks: list[DocumentChunk]) -> list[EmbeddingResult]:
        ... 
        