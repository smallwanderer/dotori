from types import SimpleNamespace
import pytest
import requests
from llm_installation.embedding_function_evaluator import evaluate_text_embed_contract
pytestmark = pytest.mark.unit

def run(change=None, fingerprint="fp", error=False, token_fixture=None):
    def post(*args, **kwargs):
        if error:
            raise requests.ConnectionError("secret at http://example.test")
        item=dict(dense_vector=[1.,0.], sparse_vector={"1": 1.}, model_name="model", backend="backend", dimension=2)
        item.update(change or {})
        payload={"results": [item,item]} if "texts" in kwargs["json"] else item.copy()
        payload["runtime_fingerprint"]=fingerprint
        return SimpleNamespace(status_code=200,json=lambda:payload)
    runtime=SimpleNamespace(max_tokens=128,dimension=2,normalize_embeddings=True,model_id="model",
        model_revision="revision",provider="backend",runtime_fingerprint="fp",supports_sparse=True,catalog_id="profile")
    return evaluate_text_embed_contract(base_url="http://example.test",runtime=runtime,model_id="catalog-model",
        backend_version="v1",evaluation_id="probe",token="secret",post=post,token_fixture=token_fixture)

def test_passing_probe_is_not_complete_evidence():
    result=run()
    assert result["passed"] and result["evidence"] is None
    assert "token_limit" in result["remaining_checks"]
    assert "secret" not in str(result) and "example.test" not in str(result)

@pytest.mark.parametrize("change", [dict(dense_vector=[float("nan"),0]), dict(dense_vector=[0,0]),
    dict(dimension=3),dict(model_name="other"),dict(sparse_vector={}),dict(dense_vector=[True,0])])
def test_bad_vectors_fail(change):
    assert not run(change)["passed"]

def test_stale_runtime_fails():
    assert not run(fingerprint="stale")["passed"]

def test_error_report_does_not_expose_credentials():
    result=run(error=True)
    assert not result["passed"]
    assert "secret" not in str(result) and "example.test" not in str(result)


def test_token_cap_fixture_is_checked_but_store_and_identity_still_required():
    result = run(token_fixture=("long " * 100, "long", 8))
    assert result["checks"]["token_limit"]
    assert result["sample_count"] == 7
    assert result["remaining_checks"] == ["index_store_compatibility", "loaded_artifact_identity"]
    assert result["evidence"] is None


def test_readiness_exposes_provider_revision(monkeypatch):
    from django.test import RequestFactory
    from documents.embedding import internal_views
    runtime = SimpleNamespace(runtime_fingerprint="test-fp", dimension=2)
    provider = SimpleNamespace(spec=SimpleNamespace(model_revision="pinned-revision"),
        healthcheck=lambda: dict(model_name="model", backend="backend", dimension=2))
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", lambda: runtime)
    monkeypatch.setattr(internal_views, "get_embedding_provider", lambda **kwargs: provider)
    internal_views._reset_readiness_cache()
    try:
        response = internal_views.readyz(RequestFactory().get("/readyz"))
        import json
        assert response.status_code == 200
        assert json.loads(response.content)["model_revision"] == "pinned-revision"
    finally:
        internal_views._reset_readiness_cache()


@pytest.mark.parametrize("index, typ, expected", [
    ("CREATE INDEX i USING hnsw (vector vector_ip_ops)", "vector(1024)", True),
    ("CREATE INDEX i USING hnsw (vector vector_cosine_ops)", "vector(1024)", False),
    (None, "vector(1024)", False),
    ("CREATE INDEX i USING hnsw (vector vector_ip_ops)", "vector(768)", False),
])
def test_store_probe_rejects_wrong_or_missing_active_index(monkeypatch, index, typ, expected):
    from documents.management.commands import evaluate_text_embed as command
    class Cursor:
        def __enter__(self):
            self.rows = iter([(typ,), (index,) if index else None])
            return self
        def __exit__(self, *args):
            pass
        def execute(self, *args):
            pass
        def fetchone(self):
            return next(self.rows)
    monkeypatch.setattr(command, "connection", SimpleNamespace(cursor=Cursor))
    runtime = SimpleNamespace(dimension=1024, store="pgvector_chunk_1024",
                              distance_strategy="inner_product", normalize_embeddings=True)
    assert command.Command._store_matches(runtime) == expected


def test_command_refuses_legacy_fallback_without_output(tmp_path):
    from django.core.management import call_command, CommandError
    with pytest.raises(CommandError, match="Explicit runtime config"):
        call_command("evaluate_text_embed", base_url="http://example.test",
            runtime_config=str(tmp_path / "missing.json"), backend_version="v1", evaluation_id="probe",
            result_output=str(tmp_path / "report.json"), evidence_output=str(tmp_path / "evidence.json"))
    assert not (tmp_path / "evidence.json").exists()


def test_identity_mismatch_writes_failure_report_only(tmp_path, monkeypatch):
    from django.core.management import call_command, CommandError
    from documents.management.commands import evaluate_text_embed as command
    import json
    entry = SimpleNamespace(repo_id="model", revision="revision", provider="backend", dimension=2,
        store="pgvector_chunk_1024", normalize_embeddings=True, distance_strategy="inner_product",
        supports_sparse=True, query_prefix="", document_prefix="", tokenizer_id="tokenizer",
        tokenizer_revision="revision")
    runtime = SimpleNamespace(catalog_id="profile", model_id="model", model_revision="revision",
        provider="backend", dimension=2, store=entry.store, normalize_embeddings=True,
        distance_strategy="inner_product", supports_sparse=True, query_prefix="", document_prefix="",
        tokenizer_id="tokenizer", tokenizer_revision="revision", runtime_fingerprint="expected")
    path = tmp_path / "runtime.json"
    path.write_text("{}")
    monkeypatch.setattr(command, "load_embedding_runtime", lambda **kw: runtime)
    monkeypatch.setattr(command, "get_embedding_catalog_entry", lambda *args: entry)
    monkeypatch.setattr(command.requests, "get", lambda *args, **kw:
        SimpleNamespace(status_code=200, json=lambda: {"status":"ready", "runtime_fingerprint":"stale"}))
    report, evidence = tmp_path / "report.json", tmp_path / "evidence.json"
    with pytest.raises(CommandError, match="identity mismatch"):
        call_command("evaluate_text_embed", base_url="http://example.test", runtime_config=str(path),
            backend_version="v1", evaluation_id="probe", result_output=str(report), evidence_output=str(evidence))
    assert json.loads(report.read_text())["passed"] is False
    assert not evidence.exists()
