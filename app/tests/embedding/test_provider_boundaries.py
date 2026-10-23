from unittest.mock import MagicMock, patch

import pytest

from capabilities.providers import validate_embedding_registration
from documents.embedding.registry import create_embedding_implementation, get_embedding_client
from documents.embedding.providers.bgem3 import BGEM3HybridProvider, coerce_dense_vector
from documents.embedding.providers.openai_compatible import OpenAIEmbeddingProvider
from documents.embedding.providers.sentence_transformers import SentenceTransformersEmbeddingProvider
from documents.services.embedding_runtime_config import get_active_embedding_runtime

pytestmark = pytest.mark.unit


def test_model_factory_cannot_run_in_request_process(monkeypatch):
    monkeypatch.delenv("DOTORI_EMBEDDING_MODEL_PROCESS", raising=False)
    with pytest.raises(RuntimeError, match="model-owning"):
        create_embedding_implementation()


def test_client_remains_remote_even_in_model_process(monkeypatch):
    from documents.embedding.providers.remote import RemoteEmbeddingProxyProvider
    monkeypatch.setenv("DOTORI_EMBEDDING_MODEL_PROCESS", "1")
    assert isinstance(get_embedding_client(), RemoteEmbeddingProxyProvider)


def test_registration_rejects_missing_implementation():
    with pytest.raises(ValueError, match="registration"):
        validate_embedding_registration({"bgem3_hybrid": object})


def test_runtime_distance_reaches_implementation(monkeypatch):
    monkeypatch.setenv("DOTORI_EMBEDDING_MODEL_PROCESS", "1")
    runtime = get_active_embedding_runtime().model_copy(update={"provider": "sentence_transformers", "distance_strategy": "cosine"})
    assert create_embedding_implementation(runtime=runtime).spec.default_distance == "cosine"


@pytest.mark.parametrize("vector", [[float("nan"), 1], [float("inf"), 1], [0, 0]])
def test_invalid_vectors_rejected(vector):
    with pytest.raises(RuntimeError, match="finite"):
        coerce_dense_vector(vector)


def test_dense_normalization_is_optional():
    assert coerce_dense_vector([3, 4], normalize=False) == [3, 4]
    assert coerce_dense_vector([3, 4]) == [0.6, 0.8]
    with pytest.raises(ValueError, match="normalized"):
        BGEM3HybridProvider(model_name="test", normalize_embeddings=False)


@pytest.mark.parametrize("indices", [[0, 0], [1, 2], [0], [True, 1]])
def test_api_response_indices_must_cover_inputs(indices):
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": [{"index": i, "embedding": [3, 4]} for i in indices]}
    provider = OpenAIEmbeddingProvider(model_name="test")
    with patch("httpx.Client.post", return_value=response), pytest.raises(RuntimeError, match="indices"):
        provider.embed_documents(["one", "two"])


def test_token_limit_restored_even_on_encode_failure(monkeypatch):
    class Model:
        max_seq_length = 512
        def encode(self, texts, **kwargs):
            assert self.max_seq_length == 32
            raise RuntimeError("encode failed")
    model = Model()
    monkeypatch.setattr("documents.embedding.providers.sentence_transformers.get_sentence_transformer_model", lambda *args: model)
    provider = SentenceTransformersEmbeddingProvider(model_name="test")
    with pytest.raises(RuntimeError, match="encode failed"):
        provider.embed_query("query", max_length=32)
    assert model.max_seq_length == 512


def test_concurrent_token_limits_do_not_change_running_encode(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    entered = Event()
    release = Event()
    second_started = Event()
    seen = []

    class Model:
        max_seq_length = 512
        def encode(self, texts, **kwargs):
            limit = self.max_seq_length
            if texts == ["first"]:
                entered.set()
                assert release.wait(5)
                assert self.max_seq_length == limit
            seen.append(limit)
            return [[3, 4]]

    model = Model()
    monkeypatch.setattr("documents.embedding.providers.sentence_transformers.get_sentence_transformer_model", lambda *args: model)
    provider = SentenceTransformersEmbeddingProvider(model_name="test", normalize_embeddings=False)
    def second():
        second_started.set()
        return provider.embed_document("second", max_length=128)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(provider.embed_query, "first", max_length=32)
        try:
            assert entered.wait(5)
            other = pool.submit(second)
            assert second_started.wait(5)
            assert model.max_seq_length == 32
        finally:
            release.set()
        assert first.result().dense_vector == [3, 4]
        assert other.result().dense_vector == [3, 4]
    assert seen == [32, 128]
    assert model.max_seq_length == 512
