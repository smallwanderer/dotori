"""Shipped embedding implementations; static metadata without model imports."""
from types import MappingProxyType

EMBEDDING_BACKENDS = MappingProxyType({
    "bgem3_hybrid": "text.embed",
    "sentence_transformers": "text.embed",
    "openai_compatible": "text.embed",
})


def validate_embedding_registration(factories):
    if set(factories) != set(EMBEDDING_BACKENDS):
        raise ValueError("Embedding implementation registration differs from shipped backends")
