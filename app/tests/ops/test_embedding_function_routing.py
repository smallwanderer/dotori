from dataclasses import replace
from types import SimpleNamespace

import pytest
from django.test import RequestFactory

from capabilities.deployment import load_deployment_snapshot
from documents.embedding import routing
from documents.embedding.providers.remote import RemoteEmbeddingProxyProvider
from documents.services.embedding_runtime_config import EmbeddingRuntimeSnapshot
from installation.deployment import build_module_deployment_plan, write_deployment_plan
from llm_installation.embedding_catalog.loader import get_embedding_catalog_entry
from llm_installation.embedding_config_store import build_embedding_runtime_payload

pytestmark = pytest.mark.unit


@pytest.fixture
def routed(monkeypatch, tmp_path):
    entry = get_embedding_catalog_entry("bge-m3-hybrid")
    runtime = EmbeddingRuntimeSnapshot.model_validate(build_embedding_runtime_payload(
        entry=entry, scope="production", generation_id="routing-test"))
    plan = build_module_deployment_plan(["documents"], embedding_runtime_config=runtime.model_dump())
    path = write_deployment_plan(plan, repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    snapshot = load_deployment_snapshot(path=path)
    monkeypatch.setattr(routing, "get_active_embedding_runtime", lambda: runtime)
    monkeypatch.setattr(routing, "load_deployment_snapshot", lambda: snapshot)
    provider = RemoteEmbeddingProxyProvider(
        model_name=runtime.model_id, backend=runtime.provider, model_revision=runtime.model_revision,
        dimension=runtime.dimension, supports_sparse=runtime.supports_sparse,
        normalize_embeddings=runtime.normalize_embeddings, query_prefix=runtime.query_prefix,
        document_prefix=runtime.document_prefix, default_distance=runtime.distance_strategy,
        runtime_fingerprint=runtime.runtime_fingerprint)
    return provider, runtime, snapshot


@pytest.mark.parametrize("fault", ["disabled", "missing", "installation", "catalog", "revision", "stale", "spec", "adapter"])
def test_route_failure_prevents_http(routed, monkeypatch, fault):
    provider, runtime, snapshot = routed
    if fault == "disabled":
        snapshot = replace(snapshot, role_bindings={**snapshot.role_bindings, "embedding": {"enabled": False}})
    elif fault == "missing":
        snapshot = replace(snapshot, function_providers={}, default_routes={})
    elif fault == "installation":
        snapshot = replace(snapshot, role_bindings={**snapshot.role_bindings, "embedding": {"enabled": True, "model_installation_id": "other"}})
    elif fault in {"catalog", "revision"}:
        key = f"embedding:{runtime.generation_id}"
        field = "catalog_id" if fault == "catalog" else "artifact_revision"
        snapshot = replace(snapshot, installed_models={key: {**snapshot.installed_models[key], field: "other"}})
    elif fault == "adapter":
        snapshot = replace(snapshot, function_providers={"text.embed": [{**snapshot.function_providers["text.embed"][0], "adapter_contract": "other"}]})
    elif fault == "stale":
        provider.runtime_fingerprint = "old"
    else:
        provider.spec = replace(provider.spec, query_prefix="other")
    monkeypatch.setattr(routing, "load_deployment_snapshot", lambda: snapshot)
    def unexpected_http(*args, **kwargs):
        pytest.fail("invalid route must not reach inference")
    monkeypatch.setattr("documents.embedding.providers.remote.requests.post", unexpected_http)
    with pytest.raises(RuntimeError, match="No usable deployment route"):
        provider.embed_query("query")


@pytest.mark.parametrize("operation", ["query", "document", "batch"])
@pytest.mark.parametrize("fault", [None, "identity", "model", "dimension", "count"])
def test_response_is_checked_before_vectors_return(routed, monkeypatch, operation, fault):
    provider, runtime, _ = routed
    identity = routing.runtime_identity(runtime)
    item = {"dense_vector": [0.0] * runtime.dimension, "sparse_vector": {},
            "model_name": runtime.model_id, "backend": runtime.provider, "dimension": runtime.dimension}
    if fault == "model":
        item["model_name"] = "other"
    if fault == "dimension":
        item["dense_vector"] = [0.1]
    payload = {"execution_identity": identity, **item}
    if operation == "batch":
        payload = {"execution_identity": identity, "results": [item, item]}
        if fault == "count":
            payload["results"] = [item]
    if fault == "identity":
        payload["execution_identity"] = {**identity, "runtime_fingerprint": "old"}
    def post(*args, **kwargs):
        assert kwargs["json"]["expected_identity"] == identity
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)
    monkeypatch.setattr("documents.embedding.providers.remote.requests.post", post)
    call = (lambda: provider.embed_documents(["a", "b"])) if operation == "batch" else (
        lambda: getattr(provider, f"embed_{operation}")("a"))
    if fault and (fault != "count" or operation == "batch"):
        with pytest.raises(RuntimeError):
            call()
    else:
        assert call()


def test_executor_rejects_identity_before_admission(routed, monkeypatch, settings):
    import json
    from documents.embedding import internal_views
    _, runtime, _ = routed
    settings.EMBEDDING_INTERNAL_TOKEN = "routing-token"
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", lambda: runtime)
    def unexpected_model(*args, **kwargs):
        pytest.fail("mismatched request must not acquire inference admission")
    monkeypatch.setattr(internal_views, "run_embedding_with_admission", unexpected_model)
    request = RequestFactory().post("/embed/", data=json.dumps({
        "input_type": "query", "text": "q", "expected_identity": {
            **routing.runtime_identity(runtime), "runtime_generation_id": "old"}}),
        content_type="application/json", HTTP_AUTHORIZATION="Bearer routing-token")
    response = internal_views.embed(request)
    assert response.status_code == 409
    assert json.loads(response.content)["error"]["code"] == "EMBEDDING_IDENTITY_MISMATCH"


def test_legacy_schema_two_keeps_role_compatibility(routed, monkeypatch):
    provider, runtime, snapshot = routed
    monkeypatch.setattr(routing, "load_deployment_snapshot", lambda: replace(
        snapshot, schema_version=2, function_providers={}, default_routes={}))
    assert routing.resolve_embedding_identity(provider.spec, provider.runtime_fingerprint) == routing.runtime_identity(runtime)


def test_executor_detects_runtime_transition_during_inference(routed, monkeypatch, settings):
    import json
    from documents.embedding import internal_views
    _, runtime, _ = routed
    settings.EMBEDDING_INTERNAL_TOKEN = "routing-token"
    current = [runtime]
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", lambda: current[0])
    def run(*args, **kwargs):
        current[0] = runtime.model_copy(update={"generation_id": "changed"})
        return []
    monkeypatch.setattr(internal_views, "run_embedding_with_admission", run)
    request = RequestFactory().post("/embed/", data=json.dumps({
        "input_type": "query", "text": "q", "expected_identity": routing.runtime_identity(runtime)}),
        content_type="application/json", HTTP_AUTHORIZATION="Bearer routing-token")
    assert internal_views.embed(request).status_code == 409


@pytest.mark.parametrize("operation", ["query", "document", "batch"])
def test_proxy_and_http_handler_identity_roundtrip(routed, monkeypatch, settings, operation):
    import json
    from documents.embedding import internal_views
    from documents.embedding.providers.base import EmbeddingResult
    provider, runtime, _ = routed
    settings.EMBEDDING_INTERNAL_TOKEN = "routing-token"
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", lambda: runtime)
    def run(input_type, *, text=None, texts=None, max_length=None):
        assert input_type == ("query" if operation == "query" else "document")
        return [EmbeddingResult(dense_vector=[0.1] * runtime.dimension, sparse_vector={},
                                model_name=runtime.model_id, backend=runtime.provider,
                                dimension=runtime.dimension) for _ in (texts or [text])]
    monkeypatch.setattr(internal_views, "run_embedding_with_admission", run)
    def post(url, *, json: dict, headers, timeout):
        request = RequestFactory().post("/embed/", data=__import__("json").dumps(json),
                                       content_type="application/json", HTTP_AUTHORIZATION=headers["Authorization"])
        response = internal_views.embed(request)
        assert response.status_code == 200
        return SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: __import__("json").loads(response.content))
    monkeypatch.setattr("documents.embedding.providers.remote.requests.post", post)
    result = provider.embed_documents(["a", "b"]) if operation == "batch" else getattr(provider, f"embed_{operation}")("a")
    assert len(result) == 2 if operation == "batch" else result.dimension == runtime.dimension


def test_timeout_keeps_existing_transport_failure_contract(routed, monkeypatch):
    import requests
    provider, _, _ = routed
    def timeout(*args, **kwargs):
        raise requests.Timeout("timeout")
    monkeypatch.setattr("documents.embedding.providers.remote.requests.post", timeout)
    with pytest.raises(RuntimeError, match="query embedding request failed"):
        provider.embed_query("q")
