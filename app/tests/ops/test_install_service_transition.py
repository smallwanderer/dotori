import importlib.util
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


def test_compose_start_failure_restores_capability_and_previous_services(
    installer, tmp_path, monkeypatch
):
    capability_path = tmp_path / "capabilities.json"
    previous_payload = b'{"previous": true}\n'
    capability_path.write_bytes(previous_payload)
    commands = []

    def run_command(command, *args, **kwargs):
        commands.append(command)
        return True, "", ""

    process = SimpleNamespace(returncode=1, communicate=lambda: None)
    monkeypatch.setattr(installer, "run_command", run_command)
    monkeypatch.setattr(installer.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(installer, "read_deployment_plan", lambda _scope: _previous_rag_plan())
    monkeypatch.setattr(installer, "capability_config_path", lambda: capability_path)
    monkeypatch.setattr(installer, "RuntimeLifecycleManager", Mock)

    assert installer.run_services("3") is False

    assert capability_path.read_bytes() == previous_payload
    assert any("wait_for_document_processing" in command for command in commands)
    assert any("stop parser-executor embedding-executor" in command for command in commands)
    assert commands[-1].endswith(
        "up --no-build --remove-orphans -d db redis app dotori-orchestrator "
        "parser-executor embedding-executor"
    )


def test_drain_failure_restores_admission_without_stopping_executors(
    installer, tmp_path, monkeypatch
):
    capability_path = tmp_path / "capabilities.json"
    previous_payload = b'{"previous": true}\n'
    capability_path.write_bytes(previous_payload)
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
    monkeypatch.setattr(installer, "capability_config_path", lambda: capability_path)
    monkeypatch.setattr(installer, "RuntimeLifecycleManager", Mock)

    assert installer.run_services("3") is False

    assert capability_path.read_bytes() == previous_payload
    assert not any("stop parser-executor" in command for command in commands)
    popen.assert_not_called()
