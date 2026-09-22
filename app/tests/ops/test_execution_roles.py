import pytest
import json
from types import SimpleNamespace

from capabilities.config import write_capability_config
from capabilities.execution import EXECUTION_ROLES, executor_url, get_execution_role, require_registered_role
from capabilities.registry import DOCUMENT_EXTRACT
from capabilities.work_registry import get_work_definition, require_registered_work
from document_ai.services.capability_admission import CapabilityAdmissionError
from document_ai.services.executor_transport import _base_url
from document_ai.orchestration import WorkKind
from document_ai.embedding.providers.remote import _embedding_service_url


pytestmark = pytest.mark.unit


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
    from document_ai.services.llm_endpoint_service import resolve_rag_llm_request_config
    from document_ai.services.rag_runtime_config import LLMRuntimeNotConfigured

    config_path = tmp_path / "capabilities.json"
    config = write_capability_config([], path=config_path)
    monkeypatch.setenv("DOTORI_CAPABILITIES_CONFIG_PATH", str(config_path))
    plan_path = tmp_path / "deployment_scopes" / "production" / "deployment_plan.json"
    plan_path.parent.mkdir(parents=True)
    plan = {
        "capability_revision": config.revision,
        "execution_roles": {},
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    job = SimpleNamespace(llm_endpoint_id=None, llm_base_url="http://local:8080", llm_model="local-model")
    with pytest.raises(LLMRuntimeNotConfigured, match="llm is not registered"):
        resolve_rag_llm_request_config(job)

    llm = get_execution_role("llm")
    plan["execution_roles"]["llm"] = {
        "transport": llm.transport,
        "config_source": llm.config_source,
        "compose_service": llm.compose_service,
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    assert require_registered_role("llm") == llm
    assert resolve_rag_llm_request_config(job).base_url == "http://local:8080"

    plan["capability_revision"] = "cap-stale"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(LLMRuntimeNotConfigured, match="revision"):
        resolve_rag_llm_request_config(job)


def test_deployment_work_route_controls_parse_publication(tmp_path, monkeypatch):
    from unittest.mock import patch
    from document_ai.orchestration import enqueue_parse

    config_path = tmp_path / "capabilities.json"
    config = write_capability_config([DOCUMENT_EXTRACT], path=config_path)
    monkeypatch.setenv("DOTORI_CAPABILITIES_CONFIG_PATH", str(config_path))
    plan_path = tmp_path / "deployment_scopes" / "production" / "deployment_plan.json"
    plan_path.parent.mkdir(parents=True)
    parser = get_execution_role("parser")
    parser_entry = {
        "transport": parser.transport,
        "config_source": parser.config_source,
        "compose_service": parser.compose_service,
    }
    plan = {
        "capability_revision": config.revision,
        "enabled_capabilities": [DOCUMENT_EXTRACT],
        "execution_roles": {"parser": parser_entry},
        "work_routes": {},
    }
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    with patch("document_ai.tasks.parse_document_with_docling.apply_async") as task:
        with pytest.raises(CapabilityAdmissionError) as missing:
            enqueue_parse(1)
        assert missing.value.code == "CAPABILITY_UNAVAILABLE"
        task.assert_not_called()

        plan["work_routes"]["parse_document"] = get_work_definition("parse_document").deployment_entry()
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        assert require_registered_work("parse_document").queue == "parse"
        enqueue_parse(1)
        assert task.call_args.kwargs["queue"] == "parse"

        plan["work_routes"]["parse_document"]["queue"] = "embed"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(CapabilityAdmissionError):
            enqueue_parse(1)
        assert task.call_count == 1


def test_queued_work_is_finalized_when_route_is_not_registered():
    from unittest.mock import patch
    from document_ai.tasks import _dispatch_or_retry

    task = SimpleNamespace(request=SimpleNamespace(headers={}, retries=0))
    with patch("document_ai.services.capability_admission.require_capability"), patch(
        "document_ai.tasks.require_registered_work", side_effect=ValueError("route missing")
    ), patch("document_ai.processing.capability_state.finalize_capability_rejection") as finalize:
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
    from document_ai import executor_views

    settings.EXECUTOR_INTERNAL_TOKEN = "test-token"
    monkeypatch.setenv("DOTORI_EXECUTOR_ROLE", "parser")
    request = RequestFactory().post(
        "/internal/v1/jobs/execute/",
        json.dumps({"job_id": "job-1", "work_kind": "parse_document", "envelope_version": 1, "payload": {"node_id": 1}}),
        content_type="application/json",
        HTTP_AUTHORIZATION="Bearer test-token",
    )
    with patch("document_ai.executor_views.require_registered_work", side_effect=ValueError("route missing")), patch(
        "document_ai.executor_views.claim_execution"
    ) as claim:
        response = executor_views._execute_job(request)
    assert response.status_code == 422
    assert json.loads(response.content)["error"]["code"] == "WORK_NOT_REGISTERED"
    claim.assert_not_called()


def test_upload_signal_keeps_file_when_parse_route_is_unavailable():
    from unittest.mock import patch
    from document_ai.signals import trigger_document_parsing

    blob = SimpleNamespace(node_id=42, node=SimpleNamespace(ai_processing_enabled=True))
    error = CapabilityAdmissionError(DOCUMENT_EXTRACT, "CAPABILITY_UNAVAILABLE", "parse route missing")
    with patch("document_ai.signals.capability_enabled", return_value=True), patch(
        "document_ai.signals.enqueue_parse", side_effect=error
    ), patch("document_ai.processing.capability_state.finalize_capability_rejection") as finalize:
        trigger_document_parsing(None, blob, True)
    finalize.assert_called_once_with(
        "parse_document", {"node_id": 42},
        error_code="CAPABILITY_UNAVAILABLE", message="parse route missing",
    )
