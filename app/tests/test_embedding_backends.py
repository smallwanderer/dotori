import os
import sys

import pytest
from django.conf import settings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

from document_ai.embedding import embeding_models
from document_ai.embedding.embeding_models import EmbeddingResult
from document_ai.embedding.store_registry import get_embedding_store_instance
from document_ai.parsers import config
from document_ai.processing import embedding as embedding_processing


def test_embedding_token_budget_uses_chunk_budget_plus_headroom():
    settings.CHUNK_MAX_TOKENS = 1024
    settings.EMBEDDING_TOKEN_HEADROOM = 192
    if hasattr(settings, "EMBEDDING_MAX_TOKENS"):
        delattr(settings, "EMBEDDING_MAX_TOKENS")

    assert config.get_chunk_max_tokens() == 1024
    assert config.get_embedding_token_headroom() == 192
    assert config.get_embedding_max_tokens() == 1216


def test_embedding_max_tokens_respects_explicit_override():
    settings.CHUNK_MAX_TOKENS = 1024
    settings.EMBEDDING_TOKEN_HEADROOM = 256
    settings.EMBEDDING_MAX_TOKENS = 1536

    assert config.get_embedding_max_tokens() == 1536


def test_embedding_input_preparation_truncates_overflow(monkeypatch, settings):
    settings.EMBEDDING_MAX_TOKENS = 5

    class FakeTokenizer:
        def count_tokens(self, text):
            return len(text.split())

    class FakeRawTokenizer:
        def encode(self, text, add_special_tokens=False):
            return text.split()

        def decode(self, token_ids, skip_special_tokens=True, clean_up_tokenization_spaces=True):
            return " ".join(token_ids)

    monkeypatch.setattr(embedding_processing, "get_hf_tokenizer", lambda: FakeTokenizer())
    monkeypatch.setattr(embedding_processing, "get_raw_tokenizer", lambda: FakeRawTokenizer())

    text, token_count, truncated = embedding_processing.prepare_embedding_input_text(
        "one two three four five six"
    )

    assert text == "one two three four five"
    assert token_count == 5
    assert truncated is True


def test_embedding_input_preparation_accepts_limit(monkeypatch, settings):
    settings.EMBEDDING_MAX_TOKENS = 5

    class FakeTokenizer:
        def count_tokens(self, text):
            return 5

    monkeypatch.setattr(embedding_processing, "get_hf_tokenizer", lambda: FakeTokenizer())

    text, token_count, truncated = embedding_processing.prepare_embedding_input_text("fits")

    assert text == "fits"
    assert token_count == 5
    assert truncated is False


def test_embedder_dispatches_to_bgem3_hybrid(monkeypatch):
    called = {}

    def fake_hybrid(**kwargs):
        called["backend"] = "bgem3_hybrid"
        return EmbeddingResult(
            dense_vector=[0.1, 0.2],
            sparse_vector={"101": 0.7},
        )

    monkeypatch.setattr(embeding_models, "_embed_with_bgem3_hybrid", fake_hybrid)

    result = embeding_models.bge_m3_embedder(
        text="hello world",
        model_name="dummy-model",
        backend="bgem3_hybrid",
        max_length=32,
    )

    assert called["backend"] == "bgem3_hybrid"
    assert result.dense_vector == [0.1, 0.2]
    assert result.sparse_vector == {"101": 0.7}


def test_document_and_query_entrypoints_share_embedder_policy(monkeypatch):
    calls = []

    def fake_embedder(**kwargs):
        calls.append(kwargs)
        return EmbeddingResult(dense_vector=[1.0], sparse_vector={})

    monkeypatch.setattr(embeding_models, "bge_m3_embedder", fake_embedder)
    settings.QUERY_EMBEDDING_MAX_TOKENS = 64

    embeding_models.embed_document(
        text="document chunk",
        model_name="dummy-model",
        backend="bgem3_hybrid",
        max_length=128,
    )
    embeding_models.embed_query(
        query="query",
        model_name="dummy-model",
        backend="bgem3_hybrid",
    )

    assert calls[0]["text"] == "document chunk"
    assert calls[0]["max_length"] == 128
    assert calls[1]["text"] == "query"
    assert calls[1]["max_length"] == 64


def test_sparse_vector_is_l2_normalized():
    normalized = embeding_models._normalize_sparse_vector(
        {"10": 3.0, "20": 4.0}
    )

    assert normalized["10"] == 0.6
    assert normalized["20"] == 0.8


def test_embedding_store_rejects_dimension_mismatch(settings):
    settings.EMBEDDING_MODEL = "dummy-model"
    settings.EMBEDDING_BACKEND = "bgem3_hybrid"
    settings.EMBEDDING_DIMENSION = 1024
    settings.EMBEDDING_STORE = "pgvector_chunk_1024"
    settings.EMBEDDING_SPARSE_ENABLED = True

    store = get_embedding_store_instance()
    embedding = EmbeddingResult(
        dense_vector=[0.1, 0.2],
        sparse_vector={"101": 1.0},
        model_name="dummy-model",
        backend="bgem3_hybrid",
        dimension=2,
    )

    with pytest.raises(ValueError, match="dimension"):
        store.validate_embedding(embedding)


def test_embedding_store_accepts_active_model_backend_and_dimension(settings):
    settings.EMBEDDING_MODEL = "dummy-model"
    settings.EMBEDDING_BACKEND = "bgem3_hybrid"
    settings.EMBEDDING_DIMENSION = 2
    settings.EMBEDDING_STORE = "pgvector_chunk_1024"
    settings.EMBEDDING_SPARSE_ENABLED = True

    store = get_embedding_store_instance()
    embedding = EmbeddingResult(
        dense_vector=[0.1, 0.2],
        sparse_vector={"101": 1.0},
        model_name="dummy-model",
        backend="bgem3_hybrid",
        dimension=2,
    )

    store.validate_embedding(embedding)
