import pytest
import json
from types import SimpleNamespace

from capabilities.deployment import clear_deployment_snapshot_cache
from capabilities.modules import DOCUMENTS_MODULE
from capabilities.execution import EXECUTION_ROLES, executor_url, get_execution_role, require_registered_role
from capabilities.registry import DOCUMENT_EXTRACT
from capabilities.work_registry import get_work_definition, require_registered_work
from documents.services.capability_admission import CapabilityAdmissionError
from documents.services.executor_transport import _base_url
from documents.orchestration import WorkKind
from documents.embedding.providers.remote import _embedding_service_url
from installation.deployment import build_module_deployment_plan, write_deployment_plan


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def active_module_plan(tmp_path, monkeypatch):
    _write_plan(tmp_path, monkeypatch)
    yield
    clear_deployment_snapshot_cache()
    from llm_installation.function_catalog import load_function_evidence
    load_function_evidence.cache_clear()


def _write_plan(tmp_path, monkeypatch, *, disabled_roles=()):
    plan = build_module_deployment_plan(
        [DOCUMENTS_MODULE], disabled_provider_roles=disabled_roles
    )
    path = write_deployment_plan(plan, repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    clear_deployment_snapshot_cache()
    return plan, path


def _write_llm_route_plan(tmp_path, monkeypatch, *, disabled_roles=()):
    from llm_installation.function_catalog import load_function_evidence

    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(exist_ok=True)
    evidence = {
        "evidence_id": "test-text-generate-evidence", "function_id": "text.generate",
        "verification": "contract_verified", "model_id": "test-model",
        "model_revision": "test-revision", "contract_version": "text-generate-v1",
        "targets": [{"target_id": "test-model", "target_revision": "test-revision",
                     "artifact_digest": "a" * 64, "backend_id": "llamacpp-cpu",
                     "backend_version": "test", "adapter_contract": "openai-compatible-chat-completions-v1"}],
        "input_preprocessing_version": "test", "output_parsing_version": "test",
        "fixture_set_id": "test", "fixture_set_revision": "test", "evaluator_version": "test",
        "executed_at": "2026-10-05T00:00:00Z", "sample_count": 1,
        "contract_checks": {"request": True}, "passed": True,
    }
    (evidence_dir / "test-text-generate-evidence.json").write_text(json.dumps(evidence))
    monkeypatch.setattr("llm_installation.function_catalog.EVIDENCE_DIR", evidence_dir)
    load_function_evidence.cache_clear()
    installation_id = "llm:test-runtime-generation"
    backend_id = "llamacpp-cpu"
    plan = build_module_deployment_plan(
        [DOCUMENTS_MODULE],
        disabled_provider_roles=disabled_roles,
        installed_models={
            installation_id: {
                "role": "llm",
                "catalog_id": "test-model",
                "artifact_revision": "test-revision",
                "artifact_digest": "a" * 64,
                "backend_id": backend_id,
                "runtime_ref": "llm_runtime.json",
            }
        },
        function_providers={
            "text.generate": [
                {
                    "model_installation_id": installation_id,
                    "role": "llm",
                    "verification": "contract_verified",
                    "evidence_ref": "test-text-generate-evidence",
                    "adapter_contract": "openai-compatible-chat-completions-v1",
                    "backend_id": backend_id,
                }
            ]
        },
        default_routes={"text.generate": installation_id},
    )
    plan_path = write_deployment_plan(plan, repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(plan_path))
    runtime_path = tmp_path / "llm_runtime.json"
    runtime_path.write_text(
        json.dumps(
            {
                "target": {
                    "generation_id": "test-runtime-generation",
                    "model": "test-model",
                    "base_url": "http://local:8080",
                    "runtime": "llama.cpp",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(runtime_path))
    from llm_installation.rag_runtime_config import clear_server_rag_target_cache

    clear_server_rag_target_cache()
    clear_deployment_snapshot_cache()
    return plan, plan_path


def test_registered_roles_keep_their_existing_execution_paths(monkeypatch):
    assert set(EXECUTION_ROLES) == {"parser", "embedding", "llm"}
    assert get_execution_role("parser").transport == "orchestrator_http"
    assert get_execution_role("embedding").transport == "orchestrator_http_and_request_http"
    assert get_execution_role("llm").transport == "request_http"

    monkeypatch.setenv("PARSER_EXECUTOR_URL", "http://parser-custom:8002/")
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", "http://embedding-custom:8001/")
    assert _base_url(WorkKind.PARSE_DOCUMENT) == "http://parser-custom:8002"
    assert _base_url(WorkKind.EMBED_DOCUMENT_BATCH) == "http://embedding-custom:8001"
    assert _embedding_service_url() == "http://embedding-custom:8001"


def test_unregistered_work_and_role_fail_closed():
    with pytest.raises(ValueError, match="Unknown execution role"):
        get_execution_role("images")
    with pytest.raises(ValueError, match="Unknown executor work kind"):
        _base_url("unknown")
    with pytest.raises(ValueError, match="no HTTP executor"):
        executor_url("llm")


def test_persisted_role_registration_controls_server_llm(tmp_path, monkeypatch):
    from documents.services.llm_endpoint_service import resolve_rag_llm_request_config
    from llm_installation.rag_runtime_config import LLMRuntimeNotConfigured

    plan, plan_path = _write_plan(
        tmp_path,
        monkeypatch,
        disabled_roles=("parser", "embedding", "llm"),
    )
    job = SimpleNamespace(llm_endpoint_id=None, llm_base_url="http://local:8080", llm_model="local-model")
    with pytest.raises(LLMRuntimeNotConfigured, match="llm is not registered"):
        resolve_rag_llm_request_config(job)

    plan, _ = _write_llm_route_plan(tmp_path, monkeypatch)
    job.llm_model = "test-model"
    job.performance_metrics = {
        "provider_resolution": {
            "source": "server_runtime",
            "selection": "deployment_default_route",
            "deployment_generation_id": plan["generation_id"],
            "runtime_generation_id": "test-runtime-generation",
            "model_installation_id": "llm:test-runtime-generation",
        }
    }
    config = resolve_rag_llm_request_config(job)
    assert config.base_url == "http://local:8080"
    assert config.model == "test-model"
    assert config.provider_resolution["selection"] == "deployment_default_route"
    assert config.provider_resolution["model_installation_id"] == "llm:test-runtime-generation"

    plan, plan_path = _write_llm_route_plan(tmp_path, monkeypatch)
    plan["capability_revision"] = "cap-stale"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    clear_deployment_snapshot_cache()
    with pytest.raises(LLMRuntimeNotConfigured, match="revision"):
        resolve_rag_llm_request_config(job)


def test_server_rag_rejects_a_turn_admitted_under_another_deployment(tmp_path, monkeypatch):
    from documents.services.llm_endpoint_service import resolve_rag_llm_request_config
    from llm_installation.rag_runtime_config import LLMRuntimeNotConfigured

    plan, _ = _write_llm_route_plan(tmp_path, monkeypatch)
    job = SimpleNamespace(
        llm_endpoint_id=None,
        llm_base_url="http://local:8080",
        llm_model="test-model",
        performance_metrics={
            "provider_resolution": {
                "source": "server_runtime",
                "selection": "deployment_default_route",
                "deployment_generation_id": "older-deployment",
                "runtime_generation_id": "test-runtime-generation",
                "model_installation_id": "llm:test-runtime-generation",
            }
        },
    )

    with pytest.raises(LLMRuntimeNotConfigured, match="deployment plan changed"):
        resolve_rag_llm_request_config(job)


def test_server_rag_admission_pins_the_resolved_route_for_generation(tmp_path, monkeypatch):
    from unittest.mock import patch

    from documents.models import UserLLMPreference
    from documents.services.llm_endpoint_service import (
        build_rag_llm_snapshot,
        resolve_rag_llm_request_config,
        split_rag_llm_snapshot,
    )

    plan, _ = _write_llm_route_plan(tmp_path, monkeypatch)
    with patch(
        "documents.services.llm_endpoint_service.UserLLMPreference.objects.select_related"
    ) as select_related:
        select_related.return_value.get.side_effect = UserLLMPreference.DoesNotExist
        snapshot = build_rag_llm_snapshot(SimpleNamespace(pk=1))

    job_fields, initial_metrics = split_rag_llm_snapshot(snapshot)
    assert "_provider_resolution" not in job_fields
    assert initial_metrics["provider_resolution"]["deployment_generation_id"] == plan[
        "generation_id"
    ]
    assert initial_metrics["provider_resolution"]["model_installation_id"] == (
        "llm:test-runtime-generation"
    )

    job = SimpleNamespace(
        llm_endpoint_id=None,
        llm_base_url=job_fields["llm_base_url"],
        llm_model=job_fields["llm_model"],
        performance_metrics=initial_metrics,
    )
    resolved = resolve_rag_llm_request_config(job)
    assert resolved.provider_resolution["deployment_generation_id"] == plan["generation_id"]
    assert resolved.provider_resolution["verification"] == "contract_verified"


def test_server_rag_admission_rejects_a_missing_function_route(tmp_path, monkeypatch):
    from unittest.mock import patch

    from documents.models import UserLLMPreference
    from documents.services.llm_endpoint_service import build_rag_llm_snapshot
    from llm_installation.rag_runtime_config import (
        LLMRuntimeNotConfigured,
        clear_server_rag_target_cache,
    )

    _write_plan(
        tmp_path,
        monkeypatch,
        disabled_roles=("parser", "embedding", "llm"),
    )
    runtime_path = tmp_path / "llm_runtime.json"
    runtime_path.write_text(
        json.dumps(
            {
                "target": {
                    "generation_id": "test-runtime-generation",
                    "model": "test-model",
                    "base_url": "http://local:8080",
                    "runtime": "llama.cpp",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(runtime_path))
    clear_server_rag_target_cache()

    with patch(
        "documents.services.llm_endpoint_service.UserLLMPreference.objects.select_related"
    ) as select_related:
        select_related.return_value.get.side_effect = UserLLMPreference.DoesNotExist
        with pytest.raises(LLMRuntimeNotConfigured, match="No usable deployment route"):
            build_rag_llm_snapshot(SimpleNamespace(pk=1))


def test_explicit_workspace_endpoint_remains_outside_local_catalog_routing():
    from documents.services.llm_endpoint_service import resolve_rag_llm_request_config

    endpoint = SimpleNamespace(
        pk="endpoint-1",
        is_active=True,
        endpoint_type="openai_compatible",
        normalized_base_url="https://llm.example.test",
        default_model="external-model",
        api_key="secret-token",
        name="Workspace endpoint",
    )
    job = SimpleNamespace(
        llm_endpoint_id="endpoint-1",
        llm_endpoint=endpoint,
        llm_base_url="https://llm.example.test",
        llm_model="external-model",
        performance_metrics={
            "provider_resolution": {
                "source": "workspace_endpoint",
                "endpoint_id": "endpoint-1",
            }
        },
    )

    config = resolve_rag_llm_request_config(job)

    assert config.base_url == "https://llm.example.test"
    assert config.model == "external-model"
    assert config.headers == {"Authorization": "Bearer secret-token"}
    assert config.provider_resolution["verification"] == "operator_declared_unverified"


def test_deployment_work_route_controls_parse_publication(tmp_path, monkeypatch):
    from unittest.mock import patch
    from documents.orchestration import enqueue_parse

    plan, plan_path = _write_plan(
        tmp_path, monkeypatch, disabled_roles=("embedding", "llm")
    )
    parser = get_execution_role("parser")
    parser_entry = {
        "transport": parser.transport,
        "config_source": parser.config_source,
        "compose_service": parser.compose_service,
    }
    plan["execution_roles"] = {"parser": parser_entry}
    plan["work_routes"] = {}
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    clear_deployment_snapshot_cache()
    with patch("documents.tasks.parse_document_with_docling.apply_async") as task:
        with pytest.raises(CapabilityAdmissionError) as missing:
            enqueue_parse(1)
        assert missing.value.code == "CAPABILITY_UNAVAILABLE"
        task.assert_not_called()

        plan["work_routes"]["parse_document"] = get_work_definition("parse_document").deployment_entry()
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        clear_deployment_snapshot_cache()
        assert require_registered_work("parse_document").queue == "parse"
        enqueue_parse(1)
        assert task.call_args.kwargs["queue"] == "parse"

        plan["work_routes"]["parse_document"]["queue"] = "embed"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        clear_deployment_snapshot_cache()
        with pytest.raises(CapabilityAdmissionError):
            enqueue_parse(1)
        assert task.call_count == 1


def test_queued_work_is_finalized_when_route_is_not_registered():
    from unittest.mock import patch
    from documents.tasks import _dispatch_or_retry

    task = SimpleNamespace(request=SimpleNamespace(headers={}, retries=0))
    with patch("orchestration.celery_adapter.require_capability"), patch(
        "orchestration.celery_adapter.require_registered_work", side_effect=ValueError("route missing")
    ), patch("documents.processing.capability_state.finalize_capability_rejection") as finalize:
        result = _dispatch_or_retry(
            task,
            WorkKind.PARSE_DOCUMENT,
            envelope={"job_id": "job-1"},
            payload={"node_id": 1},
        )
    assert result["status"] == "failed"
    assert result["error_code"] == "CAPABILITY_UNAVAILABLE"
    finalize.assert_called_once()


def test_executor_rejects_unregistered_work_before_claim(settings, monkeypatch):
    from unittest.mock import patch
    from django.test import RequestFactory
    from documents import executor_views

    settings.EXECUTOR_INTERNAL_TOKEN = "test-token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "parser")
    request = RequestFactory().post(
        "/internal/v1/jobs/execute/",
        json.dumps({"job_id": "job-1", "work_kind": "parse_document", "envelope_version": 1, "payload": {"node_id": 1}}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )
    with patch("orchestration.executor_views.require_registered_work", side_effect=ValueError("route missing")), patch(
        "orchestration.receipts.claim_execution"
    ) as claim:
        response = executor_views._execute_job(request)
    assert response.status_code == 422
    assert json.loads(response.content)["error"]["code"] == "WORK_NOT_REGISTERED"
    claim.assert_not_called()


@pytest.mark.django_db(transaction=True)
def test_upload_signal_keeps_file_when_parse_route_is_unavailable():
    from unittest.mock import patch
    from documents.signals import trigger_document_parsing

    blob = SimpleNamespace(
        node_id=42,
        node=SimpleNamespace(ai_processing_enabled=True),
        original_name="sample.pdf",
        mime_type="application/pdf",
        file=SimpleNamespace(name="sample.pdf"),
    )
    error = CapabilityAdmissionError(DOCUMENT_EXTRACT, "CAPABILITY_UNAVAILABLE", "parse route missing")
    with patch("documents.signals.capability_enabled", return_value=True), patch(
        "documents.signals.enqueue_parse", side_effect=error
    ), patch("documents.processing.capability_state.finalize_capability_rejection") as finalize:
        trigger_document_parsing(None, blob, True)
    finalize.assert_called_once_with(
        "parse_document", {"node_id": 42},
        error_code="CAPABILITY_UNAVAILABLE", message="parse route missing",
    )
