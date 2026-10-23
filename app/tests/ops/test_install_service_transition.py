import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture
def installer():
    path = Path("/workspace/install.py")
    if not path.exists():
        pytest.skip("install.py is not mounted into the test service")
    spec = importlib.util.spec_from_file_location("dotori_installer_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _previous_rag_plan():
    return {
        "core_services": ["db", "redis", "app", "dotori-orchestrator"],
        "workers": [
            {"compose_service": "parser-executor", "enabled": True},
            {"compose_service": "embedding-executor", "enabled": True},
        ],
    }


def test_compose_start_failure_restores_deployment_and_previous_services(
    installer, tmp_path, monkeypatch
):
    deployment_path = tmp_path / "deployment_plan.json"
    previous_payload = b'{"previous": true}\n'
    deployment_path.write_bytes(previous_payload)
    commands = []

    def run_command(command, *args, **kwargs):
        commands.append(command)
        return True, "", ""

    process = SimpleNamespace(returncode=1, communicate=lambda: None)
    monkeypatch.setattr(installer, "run_command", run_command)
    monkeypatch.setattr(installer.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(installer, "read_deployment_plan", lambda _scope: _previous_rag_plan())
    monkeypatch.setattr(installer, "deployment_plan_path", lambda _scope: deployment_path)
    monkeypatch.setattr(
        installer,
        "write_deployment_plan",
        lambda plan: deployment_path.write_text(json.dumps(plan), encoding="utf-8"),
    )
    monkeypatch.setattr(installer, "RuntimeLifecycleManager", Mock)

    assert installer.run_services("3") is False

    assert deployment_path.read_bytes() == previous_payload
    assert any("wait_for_document_processing" in command for command in commands)
    assert any("stop parser-executor embedding-executor" in command for command in commands)
    assert commands[-1].endswith(
        "up --no-build --remove-orphans -d db redis app dotori-orchestrator "
        "parser-executor embedding-executor"
    )


def test_drain_failure_restores_deployment_without_stopping_executors(
    installer, tmp_path, monkeypatch
):
    deployment_path = tmp_path / "deployment_plan.json"
    previous_payload = b'{"previous": true}\n'
    deployment_path.write_bytes(previous_payload)
    commands = []

    def run_command(command, *args, **kwargs):
        commands.append(command)
        if "wait_for_document_processing" in command:
            return False, "", "active work remains"
        return True, "", ""

    popen = Mock()
    monkeypatch.setattr(installer, "run_command", run_command)
    monkeypatch.setattr(installer.subprocess, "Popen", popen)
    monkeypatch.setattr(installer, "read_deployment_plan", lambda _scope: _previous_rag_plan())
    monkeypatch.setattr(installer, "deployment_plan_path", lambda _scope: deployment_path)
    monkeypatch.setattr(
        installer,
        "write_deployment_plan",
        lambda plan: deployment_path.write_text(json.dumps(plan), encoding="utf-8"),
    )
    monkeypatch.setattr(installer, "RuntimeLifecycleManager", Mock)

    assert installer.run_services("3") is False

    assert deployment_path.read_bytes() == previous_payload
    assert not any("stop parser-executor" in command for command in commands)
    popen.assert_not_called()


def test_final_runtime_revision_recreates_services(
    installer, tmp_path, monkeypatch
):
    deployment_path = tmp_path / "deployment_plan.json"
    written_plans = []
    commands = []

    def run_command(command, *args, **kwargs):
        commands.append(command)
        return True, "", ""

    def write_plan(plan):
        written_plans.append(plan)
        deployment_path.write_text(json.dumps(plan), encoding="utf-8")

    process = SimpleNamespace(returncode=0, communicate=lambda: None)
    monkeypatch.setattr(installer, "run_command", run_command)
    monkeypatch.setattr(installer.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(installer, "read_deployment_plan", lambda _scope: None)
    monkeypatch.setattr(installer, "deployment_plan_path", lambda _scope: deployment_path)
    monkeypatch.setattr(installer, "write_deployment_plan", write_plan)
    monkeypatch.setattr(installer, "_runtime_start_info", lambda _scope: None)
    monkeypatch.setattr(installer, "RuntimeLifecycleManager", Mock)

    assert installer.run_services("rag") is True

    assert len(written_plans) == 2
    assert written_plans[-1]["provider_bindings"]["llm"]["enabled"] is False
    assert any("--force-recreate" in command for command in commands)


def test_final_revision_sync_failure_restores_previous_plan(
    installer, tmp_path, monkeypatch
):
    deployment_path = tmp_path / "deployment_plan.json"
    previous_payload = b'{"previous": true}\n'
    deployment_path.write_bytes(previous_payload)
    previous_plan = _previous_rag_plan()

    def run_command(command, *args, **kwargs):
        if "--force-recreate" in command:
            return False, "", "refresh failed"
        return True, "", ""

    process = SimpleNamespace(returncode=0, communicate=lambda: None)
    monkeypatch.setattr(installer, "run_command", run_command)
    monkeypatch.setattr(installer.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(installer, "read_deployment_plan", lambda _scope: previous_plan)
    monkeypatch.setattr(installer, "deployment_plan_path", lambda _scope: deployment_path)
    monkeypatch.setattr(
        installer,
        "write_deployment_plan",
        lambda plan: deployment_path.write_text(json.dumps(plan), encoding="utf-8"),
    )
    monkeypatch.setattr(installer, "_runtime_start_info", lambda _scope: None)
    monkeypatch.setattr(installer, "RuntimeLifecycleManager", Mock)

    assert installer.run_services("rag") is False
    assert deployment_path.read_bytes() == previous_payload
