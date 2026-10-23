import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

from installation.execution_observation import collect_installation_observations, observation_summary


def test_collects_enabled_services_without_changing_runtime():
    observations = {"embedding:g1": {"text.embed": {"runtime_generation_id": "g1"}}}
    manager = Mock()
    manager._compose.return_value = SimpleNamespace(returncode=0, stdout=json.dumps(observations))
    manager.observe_backend.return_value = {"status": "backend_observed", "backend_version": "1.2", "request_settings_observed": False}
    spec = object()
    result = collect_installation_observations(manager, services=["embedding-executor"], runtime_spec=spec)
    assert result["embedding"]["observations"] == observations
    assert result["llm"]["request_settings_observed"] is False
    manager.observe_backend.assert_called_once_with(spec)
    manager.start.assert_not_called()
    assert "observations" not in observation_summary(result)["embedding"]


def test_disabled_roles_are_not_probed():
    manager = Mock()
    assert collect_installation_observations(manager, services=["app"]) == {}
    manager._compose.assert_not_called()
    manager.observe_backend.assert_not_called()


def test_failed_observation_does_not_expose_stderr_or_stop_startup():
    manager = Mock()
    manager._compose.return_value = SimpleNamespace(returncode=1, stdout="", stderr="secret-token")
    report = collect_installation_observations(manager, services=["embedding-executor"])
    assert report["embedding"]["status"] == "unavailable"
    assert "secret-token" not in json.dumps(report)


def test_idle_llm_is_reported_without_wakeup():
    manager = Mock()
    manager.observe_backend.return_value = {"status": "unavailable", "reason": "runtime_not_running"}
    report = collect_installation_observations(manager, services=[], runtime_spec=object())
    assert report["llm"]["reason"] == "runtime_not_running"
    manager.start.assert_not_called()


def test_timeout_is_diagnostic():
    manager = Mock()
    manager._compose.side_effect = subprocess.TimeoutExpired("probe", 30)
    assert collect_installation_observations(manager, services=["embedding-executor"])["embedding"]["status"] == "unavailable"
