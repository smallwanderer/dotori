import hashlib
import json
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command, CommandError

from documents.embedding.candidate_validation import validate_candidate
from documents.embedding.providers.base import EmbeddingResult
from documents.embedding.providers.openai_compatible import OpenAIEmbeddingProvider
from documents.embedding.providers.sentence_transformers import SentenceTransformersEmbeddingProvider
from documents.embedding.registry import create_embedding_implementation
from documents.services.embedding_runtime_config import EmbeddingRuntimeSnapshot
from llm_installation.embedding_catalog.loader import get_embedding_catalog_entry, resolve_embedding_profile
from llm_installation.embedding_catalog.models import EmbeddingModelEntry, EmbeddingProfileEntry
from llm_installation.embedding_config_store import build_embedding_runtime_payload

pytestmark = pytest.mark.unit


def runtime(provider="sentence_transformers", **updates):
    entry = get_embedding_catalog_entry("all-minilm-l6-v2")
    payload = build_embedding_runtime_payload(scope="candidate", generation_id="test", entry=entry)
    payload.update(provider=provider, functions=[], **updates)
    return EmbeddingRuntimeSnapshot.model_validate(payload)


@pytest.mark.parametrize("options", [
    {"unknown": True}, {"trust_remote_code": "yes"}, {"truncate_dim": 0},
    {"truncate_dim": 1024}, {"query_task": ""}, {"send_dimensions": True},
])
def test_invalid_options_rejected_before_model_loading(options):
    with pytest.raises(ValueError):
        runtime(provider_options=options)


def test_prompt_name_and_prefix_cannot_both_apply():
    with pytest.raises(ValueError, match="mutually exclusive"):
        runtime(query_prefix="query: ", provider_options={"query_prompt_name": "query"})


def test_options_participate_in_fingerprint_and_old_payload_is_unchanged():
    original = runtime()
    payload = original.model_dump(exclude={"runtime_fingerprint", "provider_options", "tokenizer_options"})
    payload["runtime_fingerprint"] = hashlib.sha256(json.dumps(payload, ensure_ascii=False,
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert EmbeddingRuntimeSnapshot.model_validate(payload).runtime_fingerprint == original.runtime_fingerprint
    changed = runtime(provider_options={"query_task": "retrieval.query"})
    assert changed.runtime_fingerprint != original.runtime_fingerprint
    payload["provider_options"] = {"trust_remote_code": True}
    with pytest.raises(ValueError, match="fingerprint"):
        EmbeddingRuntimeSnapshot.model_validate(payload)


def test_explicit_null_options_survive_a_host_signed_runtime():
    payload = runtime("openai_compatible").model_dump(exclude={"runtime_fingerprint", "tokenizer_options"})
    payload["provider_options"] = {"send_dimensions": None}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["runtime_fingerprint"] = hashlib.sha256(canonical.encode()).hexdigest()
    snap = EmbeddingRuntimeSnapshot.model_validate(payload)
    assert snap.provider_options == {"send_dimensions": None}


@pytest.mark.parametrize("send_dimensions", [True, False])
def test_api_dimension_reduction_requires_an_explicit_option(send_dimensions):
    model = EmbeddingModelEntry(id="api-model", display_name="API model", repo_id="model",
        revision="external", tokenizer_id="model", tokenizer_revision="external",
        dimension=3072, model_input_max_tokens=8192)
    profile = EmbeddingProfileEntry(id="api-profile", model_id=model.id, provider="openai_compatible",
        dimension=1536, store="pgvector_chunk_1536", supports_sparse=False,
        normalize_embeddings=True, distance_strategy="inner_product", availability="experimental",
        provider_options={"send_dimensions": send_dimensions})
    if send_dimensions:
        assert resolve_embedding_profile(model, profile).dimension == 1536
    else:
        with pytest.raises(ValueError, match="dimension"):
            resolve_embedding_profile(model, profile)


def test_truncated_profile_resolves_and_options_reach_factory(monkeypatch):
    entry = get_embedding_catalog_entry("all-minilm-l6-v2")
    model_data = {k: getattr(entry, k) for k in ("display_name", "repo_id", "revision", "tokenizer_id", "tokenizer_revision", "model_input_max_tokens")}
    model = EmbeddingModelEntry(id="new-model", dimension=768, **model_data)
    profile = EmbeddingProfileEntry(id="new-profile", model_id=model.id, provider="sentence_transformers",
        dimension=384, store="pgvector_chunk_384", supports_sparse=False, normalize_embeddings=True,
        distance_strategy="inner_product", availability="experimental", provider_options={"truncate_dim": 384})
    resolved = resolve_embedding_profile(model, profile)
    snap = EmbeddingRuntimeSnapshot.model_validate(build_embedding_runtime_payload(scope="candidate", generation_id="new", entry=resolved))
    monkeypatch.setenv("DOTORI_EMBEDDING_MODEL_PROCESS", "1")
    provider = create_embedding_implementation(runtime=snap)
    assert provider.options.truncate_dim == 384
    assert provider.max_tokens == model.model_input_max_tokens
    with pytest.raises(ValueError, match="dimension"):
        resolve_embedding_profile(model, profile.model_copy(update={"dimension": 1024}))


def test_st_task_prompt_and_truncation_are_forwarded_and_limit_restored(monkeypatch):
    captured = []
    class Model:
        max_seq_length = 128
        def encode(self, texts, **kwargs):
            captured.append((texts, self.max_seq_length, kwargs))
            return [[3, 4]]
    model = Model()
    load = MagicMock(return_value=model)
    monkeypatch.setattr("documents.embedding.providers.sentence_transformers.get_sentence_transformer_model", load)
    provider = SentenceTransformersEmbeddingProvider(model_name="custom/model", model_revision="abcdef0",
        dimension=2, provider_options={"trust_remote_code": True, "truncate_dim": 2,
        "query_task": "retrieval.query", "query_prompt_name": "retrieval.query",
        "document_task": "retrieval.passage", "document_prompt_name": "retrieval.passage"})
    provider.embed_query("query", max_length=32)
    provider.embed_document("document", max_length=999)
    assert load.call_args.kwargs == {"trust_remote_code": True}
    assert captured[0][1] == 32
    assert captured[0][2]["task"] == captured[0][2]["prompt_name"] == "retrieval.query"
    assert captured[0][2]["truncate_dim"] == 2
    assert captured[1][1] == 128
    assert captured[1][2]["task"] == "retrieval.passage"
    assert model.max_seq_length == 128


def test_cache_separates_trusted_and_untrusted_loads(monkeypatch):
    import sys
    import documents.embedding.providers.sentence_transformers as module
    factory = MagicMock(side_effect=lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=factory))
    monkeypatch.setattr(module, "_ST_MODEL_CACHE", {})
    monkeypatch.setattr(module, "_ensure_torch", lambda: None)
    monkeypatch.setattr(module, "_DEVICE", None)
    one = module.get_sentence_transformer_model("new", "abcdef0")
    two = module.get_sentence_transformer_model("new", "abcdef0", trust_remote_code=True)
    assert one is not two
    assert module.get_sentence_transformer_model("new", "abcdef0") is one
    assert factory.call_count == 2


@pytest.mark.parametrize("name, flag, expected", [
    ("another-model", True, True), ("text-embedding-3-small", False, False),
    ("another-model", None, False), ("text-embedding-3-small", None, True),
])
def test_api_dimensions_are_explicit_and_legacy_default_survives(name, flag, expected):
    options = {} if flag is None else {"send_dimensions": flag}
    provider = OpenAIEmbeddingProvider(model_name=name, dimension=2, provider_options=options)
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": [{"index": 0, "embedding": [3, 4]}]}
    with patch("httpx.Client.post", return_value=response) as post:
        provider.embed_query("query")
    assert ("dimensions" in post.call_args.kwargs["json"]) == expected


def result(snap, *, dimension=None):
    dim = dimension or snap.dimension
    return EmbeddingResult(dense_vector=[1.0] + [0.0] * (dim-1), sparse_vector={},
        model_name=snap.model_id, backend=snap.provider, dimension=dim)


def test_config_only_never_constructs_a_model(monkeypatch):
    monkeypatch.delenv("DOTORI_EMBEDDING_MODEL_PROCESS", raising=False)
    with patch("documents.embedding.candidate_validation.create_embedding_implementation") as factory:
        report = validate_candidate(runtime(), config_only=True)
    factory.assert_not_called()
    assert report["passed"]
    assert "model_execution" in report["remaining_checks"]


def test_execution_outside_model_owner_is_rejected(monkeypatch):
    monkeypatch.delenv("DOTORI_EMBEDDING_MODEL_PROCESS", raising=False)
    report = validate_candidate(runtime())
    assert not report["passed"]
    assert report["checks"]["model_process"] is False


@pytest.mark.parametrize("bad_kind", ["dimension", "nan", "normalization", "sparse", "batch_count", "batch_order"])
def test_output_contract_failures_are_reported(bad_kind):
    snap = runtime("openai_compatible")
    provider = MagicMock()
    good = result(snap)
    bad = result(snap)
    if bad_kind == "dimension":
        bad = result(snap, dimension=2)
    if bad_kind == "nan":
        bad.dense_vector[0] = float("nan")
    if bad_kind == "normalization":
        bad.dense_vector[0] = 3
    if bad_kind == "sparse":
        bad.sparse_vector = {"token": 1}
    provider.embed_query.return_value = good
    provider.embed_document.return_value = bad if bad_kind not in {"batch_count", "batch_order"} else good
    provider.embed_documents.return_value = [good] if bad_kind == "batch_count" else [good, good]
    if bad_kind == "batch_order":
        other = result(snap)
        other.dense_vector = [0, 1] + [0] * (snap.dimension-2)
        provider.embed_documents.return_value = [other, good]
    with patch("documents.embedding.candidate_validation.create_embedding_implementation", return_value=provider):
        report = validate_candidate(snap)
    assert not report["passed"]
    assert report["failures"]


def test_external_pass_does_not_claim_token_limit_or_revision_verification():
    snap = runtime("openai_compatible")
    provider = MagicMock()
    provider.embed_query.return_value = provider.embed_document.return_value = result(snap)
    provider.embed_documents.return_value = [result(snap), result(snap)]
    with patch("documents.embedding.candidate_validation.create_embedding_implementation", return_value=provider):
        report = validate_candidate(snap)
    assert report["passed"]
    assert {"external_token_limit", "external_model_revision", "retrieval_quality"} <= set(report["remaining_checks"])


@pytest.mark.parametrize("ignore_cap", [False, True])
def test_local_token_probe_detects_a_provider_ignoring_the_cap(ignore_cap):
    import math
    snap = runtime()
    class Tokenizer:
        init_kwargs = {"_commit_hash": snap.tokenizer_revision}
        def __call__(self, text, *, truncation=False, max_length=None):
            tokens = text.split()
            if truncation:
                tokens = tokens[:max_length]
            return {"input_ids": tokens}
    class Model:
        tokenizer = Tokenizer()
        max_seq_length = 128
        def __getitem__(self, key):
            return SimpleNamespace(auto_model=SimpleNamespace(config=SimpleNamespace(_commit_hash=snap.model_revision)))
    class Provider:
        backend = "sentence_transformers"
        options = SimpleNamespace(document_prompt_name=None)
        def embed_document(self, text, *, max_length):
            count = len(text.split()) if ignore_cap else min(len(text.split()), max_length)
            vector = [1, count / 100]
            norm = math.sqrt(sum(v*v for v in vector))
            output = result(snap)
            output.dense_vector = [v/norm for v in vector] + [0] * (snap.dimension-2)
            return output
        embed_query = embed_document
        def embed_documents(self, texts, *, max_length):
            return [self.embed_document(text, max_length=max_length) for text in texts]
    provider = Provider()
    with patch("documents.embedding.candidate_validation.create_embedding_implementation", return_value=provider), \
         patch("documents.embedding.candidate_validation._local_model", return_value=Model()):
        report = validate_candidate(snap)
    assert report["checks"]["token_limit"] is not ignore_cap
    assert report["passed"] is not ignore_cap
    if not ignore_cap:
        assert report["observed_model_revision"] == snap.model_revision


def test_command_reads_an_out_of_catalog_pair_and_creates_only_report(tmp_path):
    entry = get_embedding_catalog_entry("all-minilm-l6-v2")
    model = {k: getattr(entry, k) for k in ("display_name", "repo_id", "revision", "tokenizer_id", "tokenizer_revision", "dimension", "model_input_max_tokens")}
    model["id"] = "custom-model"
    profile = {k: getattr(entry, k) for k in ("provider", "store", "dimension", "supports_sparse", "normalize_embeddings", "distance_strategy")}
    profile.update(id="custom-profile", model_id="custom-model", availability="experimental")
    model_path, profile_path, output = [tmp_path / name for name in ("model.json", "profile.json", "report.json")]
    model_path.write_text(json.dumps(model))
    profile_path.write_text(json.dumps(profile))
    before = (model_path.read_bytes(), profile_path.read_bytes())
    call_command("validate_embedding_candidate", model_file=model_path, profile_file=profile_path,
        result_output=output, config_only=True, stdout=StringIO())
    report = json.loads(output.read_text())
    assert report["passed"] and report["candidate"]["catalog_id"] == "custom-profile"
    assert (model_path.read_bytes(), profile_path.read_bytes()) == before
    assert set(p.name for p in tmp_path.iterdir()) == {"model.json", "profile.json", "report.json"}
    with pytest.raises(CommandError, match="new result-output"):
        call_command("validate_embedding_candidate", catalog_id=entry.id, result_output=output, config_only=True)


def test_invalid_profile_saves_failure_report_without_loading(tmp_path):
    model, output = tmp_path / "bad.json", tmp_path / "report.json"
    model.write_text("{}")
    with patch("documents.embedding.candidate_validation.create_embedding_implementation") as factory:
        with pytest.raises(CommandError, match="validation failed"):
            call_command("validate_embedding_candidate", model_file=model, profile_file=model,
                result_output=output, stdout=StringIO())
    factory.assert_not_called()
    assert json.loads(output.read_text())["passed"] is False
