import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command, CommandError
from io import StringIO

from documents.embedding.input_budget import fit_embedding_text, sentence_transformer_prompt
from documents.embedding.candidates import resolve_candidate
from documents.embedding.candidate_validation import validate_candidate
from documents.embedding.providers.base import EmbeddingResult
from documents.parsers.tokenizers import load_runtime_tokenizer, LoadedTokenizer
from documents.parsers.candidate_validation import validate_parser_candidate, parser_report_digest
from documents.services.embedding_runtime_config import EmbeddingRuntimeSnapshot

pytestmark = pytest.mark.unit


class Tokenizer:
    init_kwargs = {"_commit_hash": "abcdef0123456789"}
    def __call__(self, text, *, truncation=False, max_length=None):
        tokens = text.split()
        return {"input_ids": tokens[:max_length] if truncation else tokens}
    def count_tokens(self, text):
        return len(text.split())


def candidate(**updates):
    runtime = resolve_candidate({"catalog_id": "all-minilm-l6-v2"})
    payload = runtime.model_dump(exclude={"runtime_fingerprint"})
    payload.update(updates)
    return EmbeddingRuntimeSnapshot.model_validate(payload)


@pytest.fixture
def fake_transformers(monkeypatch):
    factory = MagicMock(return_value=Tokenizer())
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoTokenizer=SimpleNamespace(from_pretrained=factory)))
    return factory


def test_explicit_runtime_and_tokenizer_permission_are_used(fake_transformers):
    snap = candidate(tokenizer_options={"trust_remote_code": True}, provider_options={"trust_remote_code": False})
    loaded = load_runtime_tokenizer(snap)
    fake_transformers.assert_called_once_with(snap.tokenizer_id, revision=snap.tokenizer_revision, trust_remote_code=True)
    assert loaded.identity()["fallback_used"] is False
    assert loaded.observed_revision == "abcdef0123456789"


def test_model_code_permission_does_not_grant_tokenizer_permission(fake_transformers):
    load_runtime_tokenizer(candidate(provider_options={"trust_remote_code": True}))
    assert "trust_remote_code" not in fake_transformers.call_args.kwargs


def test_candidate_tokenizer_never_falls_back(fake_transformers):
    fake_transformers.side_effect = RuntimeError("missing candidate files")
    with pytest.raises(RuntimeError, match="Cannot load candidate tokenizer"):
        load_runtime_tokenizer(candidate())
    assert fake_transformers.call_count == 1


def test_compatible_fallback_reports_actual_identity_and_drops_permissions(fake_transformers):
    fake_transformers.side_effect = [RuntimeError("missing candidate"), Tokenizer()]
    loaded = load_runtime_tokenizer(candidate(tokenizer_options={"trust_remote_code": True}), allow_fallback=True)
    assert loaded.fallback_used and loaded.fallback_reason == "missing candidate"
    assert loaded.loaded_id == fake_transformers.call_args.args[0]
    assert "trust_remote_code" not in fake_transformers.call_args.kwargs


def test_empty_tokenizer_options_preserve_old_runtime_hash():
    snap = candidate()
    payload = snap.model_dump(exclude={"runtime_fingerprint", "tokenizer_options", "provider_options"})
    payload["runtime_fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    restored = EmbeddingRuntimeSnapshot.model_validate(payload)
    assert restored.runtime_fingerprint == snap.runtime_fingerprint
    assert candidate(tokenizer_options={"trust_remote_code": True}).runtime_fingerprint != snap.runtime_fingerprint
    round_trip = EmbeddingRuntimeSnapshot.model_validate(snap.model_dump(mode="json"))
    assert round_trip.runtime_fingerprint == snap.runtime_fingerprint


@pytest.mark.parametrize("options", [{"task": "query"}, {"trust_remote_code": "yes"}])
def test_invalid_tokenizer_options_rejected(options):
    with pytest.raises(ValueError):
        candidate(tokenizer_options=options)


def test_named_prompt_is_counted_and_required_prefix_preserved():
    tokenizer = Tokenizer()
    text, count, truncated = fit_embedding_text("passage: one two three four five", tokenizer,
        max_tokens=5, prompt="model prompt ", required_prefix="passage: ")
    assert text.startswith("passage: ") and truncated and count <= 5
    assert len(tokenizer("model prompt " + text)["input_ids"]) <= 5
    with pytest.raises(ValueError, match="leaves no token budget"):
        fit_embedding_text("one two", tokenizer, max_tokens=2, prompt="huge prompt ")
    model = SimpleNamespace(prompts={"query": "query: "}, default_prompt_name="query")
    assert sentence_transformer_prompt(model, {}) == "query: "
    assert sentence_transformer_prompt(model, {"prompt": ""}) == ""
    with pytest.raises(ValueError, match="Unknown model prompt"):
        sentence_transformer_prompt(model, {"prompt_name": "absent"})


def test_parser_report_composes_docling_with_explicit_tokenizer(monkeypatch):
    snap = candidate(tokenizer_revision="abcdef0")
    loaded = LoadedTokenizer(Tokenizer(), snap.tokenizer_id, snap.tokenizer_revision,
        snap.tokenizer_id, snap.tokenizer_revision, "abcdef0123456789")
    monkeypatch.setattr("documents.parsers.candidate_validation.load_runtime_tokenizer", lambda *args, **kw: loaded)
    monkeypatch.setattr("documents.parsers.candidate_validation.wrap_chunk_tokenizer", lambda *args, **kw: Tokenizer())
    # Docling composition is exercised with a deterministic chunker fixture.
    class Chunker:
        def __init__(self, **kwargs):
            assert kwargs["merge_peers"] is True
        def chunk(self, doc):
            return [" First section. ", " Second section. "]
        def contextualize(self, *, chunk):
            return chunk
    import docling.chunking
    monkeypatch.setattr(docling.chunking, "HybridChunker", Chunker)
    report = validate_parser_candidate(snap)
    assert report["passed"] and len(report["chunks"]) == 2
    assert report["tokenizer"]["fallback_used"] is False
    assert report["report_digest"] == parser_report_digest(report)
    assert "file_conversion" in report["remaining_checks"]
    assert not validate_parser_candidate(snap, headroom=snap.max_tokens)["passed"]
    assert not validate_parser_candidate(snap, max_chunks=1)["passed"]


def parser_report(snap):
    report = {"schema_version": 1, "kind": "parser_candidate", "passed": True,
        "candidate": snap.model_dump(mode="json"), "tokenizer": {"fallback_used": False, "loaded_id": snap.tokenizer_id},
        "chunks": [{"text": "one two three four five six seven", "tokens": 7}],
        "remaining_checks": ["embedding_execution", "retrieval_quality"]}
    report["report_digest"] = parser_report_digest(report)
    return report


def provider_for(snap):
    provider = MagicMock()
    output = EmbeddingResult([1.0] + [0] * (snap.dimension-1), {}, snap.model_id, snap.provider, snap.dimension)
    provider.embed_document.return_value = provider.embed_query.return_value = output
    provider.embed_documents.side_effect = lambda inputs, **kw: [output for _ in inputs]
    return provider


@pytest.mark.parametrize("bad", [None, "digest", "candidate", "fallback", "empty"])
def test_parser_report_is_bound_to_candidate_before_embedding(bad):
    snap = candidate(provider="openai_compatible", max_tokens=5, document_prefix="passage: ")
    report = parser_report(snap)
    if bad == "digest":
        report["chunks"][0]["text"] = "changed"
    if bad == "candidate":
        other = candidate(provider="openai_compatible", max_tokens=6)
        report = parser_report(other)
    if bad == "fallback":
        report["tokenizer"]["fallback_used"] = True
    if bad == "empty":
        report["chunks"] = []
    if bad in {"fallback", "empty"}:
        report["report_digest"] = parser_report_digest(report)
    provider = provider_for(snap)
    with patch("documents.embedding.candidate_validation.create_embedding_implementation", return_value=provider), \
         patch("documents.parsers.tokenizers.load_runtime_tokenizer", return_value=SimpleNamespace(tokenizer=Tokenizer())):
        result = validate_candidate(snap, parser_report=report)
    assert result["checks"]["parser_pipeline"] is (bad is None)
    if bad is None:
        assert result["parser_pipeline"]["truncated_chunks"] == 1
        assert "parser_pipeline" not in result["remaining_checks"]
    else:
        provider.embed_documents.assert_not_called()


def test_parser_command_failure_writes_a_report_without_model_loading(tmp_path):
    output = tmp_path / "parser.json"
    with patch("documents.parsers.candidate_validation.load_runtime_tokenizer", side_effect=RuntimeError("missing tokenizer")), \
         patch("documents.embedding.candidate_validation.create_embedding_implementation") as factory:
        with pytest.raises(CommandError, match="Parser candidate"):
            call_command("validate_embedding_parser_candidate", catalog_id="all-minilm-l6-v2",
                result_output=output, stdout=StringIO())
    factory.assert_not_called()
    assert json.loads(output.read_text())["checks"]["tokenizer_loading"] is False


def test_active_tokenizer_cache_is_shared_and_can_be_cleared(monkeypatch):
    from documents.parsers import config
    snap = candidate()
    loaded = LoadedTokenizer(Tokenizer(), snap.tokenizer_id, snap.tokenizer_revision,
        "fallback/tokenizer", "revision", None, True, "candidate unavailable")
    factory = MagicMock(return_value=loaded)
    monkeypatch.setattr("documents.services.embedding_runtime_config.get_active_embedding_runtime", lambda: snap)
    monkeypatch.setattr("documents.parsers.tokenizers.load_runtime_tokenizer", factory)
    config.clear_parser_tokenizer_cache()
    try:
        assert config.get_raw_tokenizer() is loaded.tokenizer
        assert config.get_raw_tokenizer() is loaded.tokenizer
        assert config.get_parser_tokenizer_status()["loaded_id"] == "fallback/tokenizer"
        assert factory.call_count == 1
        config.clear_parser_tokenizer_cache()
        assert config.get_raw_tokenizer() is loaded.tokenizer
        assert factory.call_count == 2
    finally:
        config.clear_parser_tokenizer_cache()


def test_provider_counts_named_prompt_before_real_encode(monkeypatch):
    from documents.embedding.providers.sentence_transformers import SentenceTransformersEmbeddingProvider
    class Model:
        tokenizer = Tokenizer()
        max_seq_length = 8
        prompts = {"passage": "model prompt "}
        def encode(self, inputs, **kwargs):
            assert self.max_seq_length == 5
            assert kwargs["prompt_name"] == "passage"
            assert all(len(self.tokenizer("model prompt " + text)["input_ids"]) <= 5 for text in inputs)
            return [[1, 0] for text in inputs]
    model = Model()
    monkeypatch.setattr("documents.embedding.providers.sentence_transformers.get_sentence_transformer_model", lambda *args, **kw: model)
    provider = SentenceTransformersEmbeddingProvider(model_name="test", dimension=2,
        provider_options={"document_prompt_name": "passage"})
    provider.embed_documents(["one two three four five six"], max_length=5)
    assert model.max_seq_length == 8
