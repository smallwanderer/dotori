from io import StringIO
import json
from types import SimpleNamespace

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import JsonResponse
from django.test import RequestFactory

from capabilities.deployment import load_deployment_snapshot, FunctionRouteError
from documents.embedding.execution_observation import observe_embedding_execution
from documents.embedding.routing import runtime_identity
from documents.services.embedding_runtime_config import EmbeddingRuntimeSnapshot
from installation.deployment import build_module_deployment_plan, write_deployment_plan
from llm_installation.embedding_catalog.loader import get_embedding_catalog_entry
from llm_installation.embedding_config_store import build_embedding_runtime_payload

pytestmark = pytest.mark.unit


@pytest.fixture
def loaded(monkeypatch):
    from documents.embedding.providers import bgem3
    runtime = EmbeddingRuntimeSnapshot.model_validate(build_embedding_runtime_payload(
        entry=get_embedding_catalog_entry("bge-m3-hybrid"), scope="production", generation_id="observed-test"))
    provider = SimpleNamespace(spec=SimpleNamespace(model_name=runtime.model_id,
        model_revision=runtime.model_revision, backend=runtime.provider, dimension=runtime.dimension,
        normalize_embeddings=runtime.normalize_embeddings, query_prefix=runtime.query_prefix,
        document_prefix=runtime.document_prefix, default_distance=runtime.distance_strategy))
    monkeypatch.setattr(bgem3, "_DEVICE", SimpleNamespace(type="cpu"))
    versions = {"FlagEmbedding": "1.4.0", "torch": "2.13.0", "transformers": "5.17.0"}
    monkeypatch.setattr("documents.embedding.execution_observation.version", versions.__getitem__)
    return runtime, provider


def test_owner_packages_and_device_are_observed(loaded):
    runtime, provider = loaded
    observed = observe_embedding_execution(runtime, provider)
    assert observed.backend_version == "FlagEmbedding-1.4.0_torch-2.13.0_transformers-5.17.0_cpu"
    assert observed.runtime_generation_id == runtime.generation_id
    assert observed.inference_options["max_tokens"] == runtime.max_tokens


def test_unloaded_device_does_not_use_hardware_guess(loaded, monkeypatch):
    from documents.embedding.providers import bgem3
    monkeypatch.setattr(bgem3, "_DEVICE", None)
    assert observe_embedding_execution(*loaded) is None


@pytest.mark.parametrize("fault", [None, "version", "missing", "active_settings"])
def test_replan_only_eligible_combination_keeps_default(loaded, tmp_path, fault):
    runtime, provider = loaded
    observation = observe_embedding_execution(runtime, provider).model_dump()
    if fault == "version":
        observation["backend_version"] = "different-build"
    payload = runtime.model_dump()
    if fault == "active_settings":
        payload["max_tokens"] = 4096
    observations = {} if fault == "missing" else {f"embedding:{runtime.generation_id}": {"text.embed": observation}}
    plan = build_module_deployment_plan(["documents"], embedding_runtime_config=payload,
                                        execution_observations=observations)
    candidate = plan["function_providers"]["text.embed"][0]
    snapshot = load_deployment_snapshot(path=write_deployment_plan(plan, repo_root=tmp_path))
    if fault:
        assert candidate["verification"] == "declared_unverified"
        assert candidate["evidence_applicability"]["status"] == "revalidation_required"
        assert "text.embed" not in plan["default_routes"]
        assert candidate["prior_evidence_ref"]
        with pytest.raises(FunctionRouteError):
            snapshot.resolve_function_provider("text.embed")
    else:
        assert candidate["verification"] == "contract_verified"
        assert snapshot.resolve_function_provider("text.embed")["evidence_applicability"]["status"] == "applicable"


def test_endpoint_requires_auth_before_readiness(loaded, monkeypatch, settings):
    from documents.embedding import internal_views
    settings.EMBEDDING_INTERNAL_TOKEN = "token"
    def unexpected(*args):
        pytest.fail("unauthenticated call must not load a model")
    monkeypatch.setattr(internal_views, "readyz", unexpected)
    assert internal_views.execution_observation(RequestFactory().get("/execution-observation/")).status_code == 401


def test_endpoint_and_collector_roundtrip(loaded, monkeypatch, settings, tmp_path):
    from documents.embedding import internal_views
    runtime, provider = loaded
    settings.EMBEDDING_INTERNAL_TOKEN = "token"
    monkeypatch.setenv("DOTORI_EMBEDDING_MODEL_PROCESS", "1")
    monkeypatch.setattr(internal_views, "get_active_embedding_runtime", lambda: runtime)
    monkeypatch.setattr(internal_views, "get_embedding_provider", lambda **kw: provider)
    monkeypatch.setattr(internal_views, "readyz", lambda request: JsonResponse({"status": "ready"}))
    command_path = "documents.management.commands.observe_embedding_execution"
    monkeypatch.setattr(command_path + ".get_active_embedding_runtime", lambda: runtime)
    monkeypatch.setattr(command_path + ".executor_url", lambda role: "http://executor")
    def get(url, headers, timeout):
        response = internal_views.execution_observation(RequestFactory().get(url, HTTP_AUTHORIZATION=headers["Authorization"]))
        assert response.status_code == 200
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: json.loads(response.content))
    monkeypatch.setattr(command_path + ".requests.get", get)
    output = tmp_path / "observed.json"
    call_command("observe_embedding_execution", result_output=str(output), stdout=StringIO())
    collected = json.loads(output.read_text())
    plan = build_module_deployment_plan(["documents"], embedding_runtime_config=runtime.model_dump(),
                                        execution_observations=collected)
    assert plan["function_providers"]["text.embed"][0]["evidence_applicability"]["status"] == "applicable"
    with pytest.raises(CommandError):
        call_command("observe_embedding_execution", result_output=str(output), stdout=StringIO())


def test_legacy_planner_forwards_observation_enforcement(loaded):
    from installation.deployment import build_legacy_module_deployment_plan
    runtime, _ = loaded
    plan = build_legacy_module_deployment_plan("search", embedding_runtime_config=runtime.model_dump(),
                                               execution_observations={})
    assert "text.embed" not in plan["default_routes"]
    assert plan["function_providers"]["text.embed"][0]["evidence_applicability"]["reasons"] == ["execution_observation_missing"]


def test_collector_rejects_executor_identity_mismatch(loaded, monkeypatch, settings):
    runtime, provider = loaded
    command_path = "documents.management.commands.observe_embedding_execution"
    settings.EMBEDDING_INTERNAL_TOKEN = "token"
    monkeypatch.setattr(command_path + ".get_active_embedding_runtime", lambda: runtime)
    monkeypatch.setattr(command_path + ".executor_url", lambda role: "http://executor")
    payload = {"execution_identity": {**runtime_identity(runtime), "runtime_fingerprint": "stale"},
               "observation": observe_embedding_execution(runtime, provider).model_dump()}
    monkeypatch.setattr(command_path + ".requests.get", lambda *a, **kw: SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: payload))
    with pytest.raises(CommandError):
        call_command("observe_embedding_execution", stdout=StringIO())


def test_unavailable_package_is_not_replaced_by_evidence_version(loaded, monkeypatch):
    from importlib.metadata import PackageNotFoundError
    def missing(name):
        raise PackageNotFoundError(name)
    monkeypatch.setattr("documents.embedding.execution_observation.version", missing)
    assert observe_embedding_execution(*loaded) is None


def test_same_observation_replan_has_stable_generation(loaded):
    runtime, provider = loaded
    observation = observe_embedding_execution(runtime, provider).model_dump()
    args = dict(embedding_runtime_config=runtime.model_dump(),
                execution_observations={f"embedding:{runtime.generation_id}": {"text.embed": observation}})
    first = build_module_deployment_plan(["documents"], **args)
    second = build_module_deployment_plan(["documents"], **args)
    assert first == second


def test_missing_observation_retains_verified_evidence_source_without_quality_failure(loaded):
    runtime, _ = loaded
    candidate = build_module_deployment_plan(["documents"], embedding_runtime_config=runtime.model_dump(),
                                             execution_observations={})["function_providers"]["text.embed"][0]
    assert candidate["prior_evidence_ref"] == "text-embed-bge-m3-hybrid-cpu-20261002"
    assert candidate["evidence_applicability"]["status"] == "revalidation_required"
    assert "quality_failed" not in str(candidate)


def test_loaded_provider_settings_must_match_declared_runtime(loaded):
    runtime, provider = loaded
    provider.spec.query_prefix = "stale-prefix"
    assert observe_embedding_execution(runtime, provider) is None
