from typing import TYPE_CHECKING, Optional
from functools import lru_cache

from django.conf import settings

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter
    from docling.chunking import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from docling_core.transforms.serializer.base import BaseSerializerProvider


def get_embedding_model():
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    return get_active_embedding_runtime().model_id

def get_embedding_backend() -> str:
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    return get_active_embedding_runtime().provider

def get_embedding_dimension() -> int | None:
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    return get_active_embedding_runtime().dimension

def get_embedding_sparse_enabled() -> bool:
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    return get_active_embedding_runtime().supports_sparse

def get_embedding_store() -> str:
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    return get_active_embedding_runtime().store

def get_parser_tokenizer_id() -> str:
    return getattr(settings, "PARSER_TOKENIZER_ID", "BAAI/bge-m3")

def get_parser_tokenizer_revision() -> str | None:
    value = getattr(settings, "PARSER_TOKENIZER_REVISION", "5617a9f")
    return value if value and value != "legacy" else None

def get_embedding_token_headroom() -> int:
    return getattr(settings, "EMBEDDING_TOKEN_HEADROOM", 128)

def get_embedding_max_tokens() -> int:
    return getattr(settings, "EMBEDDING_MAX_TOKENS", 1280)

def get_chunk_max_tokens() -> int:
    chunk_max_tokens = get_embedding_max_tokens() - get_embedding_token_headroom()
    if chunk_max_tokens <= 0:
        raise ValueError(
            "EMBEDDING_MAX_TOKENS must be greater than "
            "EMBEDDING_TOKEN_HEADROOM."
        )
    return chunk_max_tokens

def get_max_tokens() -> int:
    return get_chunk_max_tokens()


@lru_cache(maxsize=1)
def get_raw_tokenizer():
    from transformers import AutoTokenizer
    from document_ai.services.embedding_runtime_config import get_active_embedding_runtime

    runtime = get_active_embedding_runtime()
    revision = (
        runtime.tokenizer_revision
        if runtime.tokenizer_revision not in {"", "legacy", "main"}
        else None
    )
    return AutoTokenizer.from_pretrained(
        runtime.tokenizer_id,
        revision=revision,
    )


@lru_cache(maxsize=1)
def get_hf_tokenizer() -> "HuggingFaceTokenizer":
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    from transformers import AutoTokenizer

    return HuggingFaceTokenizer(
        tokenizer=AutoTokenizer.from_pretrained(
            get_parser_tokenizer_id(),
            revision=get_parser_tokenizer_revision(),
        ),
        max_tokens=get_chunk_max_tokens(),
    )

@lru_cache(maxsize=1)
def get_converter() -> "DocumentConverter":
    from docling.document_converter import DocumentConverter

    return DocumentConverter()


def get_hybrid_hf_chunker(
    serializer_provider: Optional["BaseSerializerProvider"] = None,
) -> "HybridChunker":
    from docling.chunking import HybridChunker

    return HybridChunker(
        tokenizer=get_hf_tokenizer(),
        # [max_tokens] Optional, default is derived from tokenizer for HF case
        # max_tokens=MAX_TOKENS, 
        merge_peers=True,
        serializer_provider=serializer_provider,
    )
