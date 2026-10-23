"""Shared delivery boundaries and legacy facade compatibility."""
import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from capabilities.deployment import clear_deployment_snapshot_cache
from installation.deployment import build_module_deployment_plan, write_deployment_plan
from orchestration import contracts, dispatch, transport, celery_adapter


@pytest.fixture(autouse=True)
def active_module_plan(tmp_path, monkeypatch):
    path = write_deployment_plan(build_module_deployment_plan(["documents"]), repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    clear_deployment_snapshot_cache()
    yield
    clear_deployment_snapshot_cache()


def test_contract_and_exception_identity_survives_legacy_imports():
    from capabilities import work_dispatch
    from documents import orchestration
    from documents.services import executor_transport

    assert work_dispatch.TaskEnvelope is orchestration.TaskEnvelope is contracts.TaskEnvelope
    assert work_dispatch.WorkContractError is contracts.WorkContractError
    assert work_dispatch.derive_job_id is orchestration.derive_job_id is contracts.derive_job_id
    assert executor_transport.ExecutorDispatchError is transport.ExecutorDispatchError
    assert executor_transport.dispatch_executor_job is transport.dispatch_executor_job


def test_task_object_publication_preserves_return_and_extra_kwargs():
    task = Mock()
    result = dispatch.dispatch_task(task, "parse_document", args=[3], kwargs={
        "job_id": "job", "trace_id": "trace", "extra": "legacy",
    })
    assert result is task.apply_async.return_value
    call = task.apply_async.call_args.kwargs
    assert call["args"] == [3] and call["task_id"] == "job" and call["queue"] == "parse"
    assert call["kwargs"]["extra"] == "legacy"
    assert call["kwargs"]["work_kind"] == "parse_document"


@pytest.mark.parametrize("kwargs", [{"work_kind": "embed_document_batch"}, {"envelope_version": 2}])
def test_mismatched_envelope_never_publishes(kwargs):
    task = Mock()
    with pytest.raises(ValueError):
        dispatch.dispatch_task(task, "parse_document", args=[1], kwargs=kwargs)
    task.apply_async.assert_not_called()


def test_transport_accepts_registered_string_without_document_enum(settings):
    settings.EXECUTOR_INTERNAL_TOKEN = "token"
    response = Mock(status_code=200)
    response.json.return_value = {"ok": True, "result": {"status": "success"}}
    with patch("orchestration.transport.requests.post", return_value=response) as post:
        assert transport.dispatch_executor_job("parse_document", envelope={"job_id": "job"}, payload={"node_id": 1}) == response.json.return_value
    assert post.call_args.kwargs["json"]["work_kind"] == "parse_document"
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer token"}


def test_rejection_uses_domain_callback_without_dispatch():
    from capabilities.admission import CapabilityAdmissionError
    task = SimpleNamespace(request=SimpleNamespace(headers={}, retries=0))
    rejection, send = Mock(), Mock()
    with patch("orchestration.celery_adapter.require_capability", side_effect=CapabilityAdmissionError("documents.extract", "CAPABILITY_DISABLED", "disabled")):
        result = celery_adapter.dispatch_or_retry(task, "parse_document", envelope={}, payload={"node_id": 1}, on_rejected=rejection, dispatch=send)
    assert result["error_code"] == "CAPABILITY_DISABLED"
    rejection.assert_called_once_with("parse_document", {"node_id": 1}, error_code="CAPABILITY_DISABLED", message="disabled")
    send.assert_not_called()


def test_common_package_never_imports_domain_or_model_code():
    root = Path(contracts.__file__).parent
    forbidden = {"documents", "files", "torch", "docling", "langgraph"}
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            assert not any(name.split(".")[0] in forbidden for name in names), path


def test_common_imports_do_not_initialize_models_or_document_handlers():
    code = """
import sys
import orchestration.contracts, orchestration.dispatch
import orchestration.transport, orchestration.celery_adapter
import orchestration.execution, orchestration.receipts, orchestration.handlers
import orchestration.executor_views, orchestration.intake
import orchestration.plans
import orchestration.submissions, orchestration.templates
assert not any(name.split('.')[0] in {'documents', 'torch', 'docling', 'langgraph'} for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], check=True)
