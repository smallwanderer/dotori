from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from llm_installation.backend_observation import parse_backend_version
from llm_installation.runtime_lifecycle import ContainerState, RuntimeLifecycleManager, build_runtime_spec

pytestmark = pytest.mark.unit


@pytest.fixture
def managed(monkeypatch, tmp_path):
    spec = build_runtime_spec("production", "llama.cpp", "test", "generation-a", repo_root=tmp_path)
    state = ContainerState(exists=True, owned=True, running=True, labels={
        "com.dotori.generation": spec.generation_id, "com.dotori.runtime": spec.runtime})
    commands = []
    def runner(args, **kwargs):
        commands.append(args)
        assert kwargs["timeout"] == 15
        if args[1] == "inspect":
            return subprocess.CompletedProcess(args, 0, stdout="container-a|sha256:image-a\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="version: 11011 (aa39d7a3e)\n", stderr="")
    manager = RuntimeLifecycleManager(repo_root=tmp_path, runner=runner)
    monkeypatch.setattr(manager, "inspect", lambda scope: state)
    return manager, spec, state, commands


def test_actual_backend_version_is_observed_without_verification_claim(managed):
    manager, spec, _, commands = managed
    result = manager.observe_backend(spec)
    assert result["status"] == "backend_observed"
    assert result["backend_version"] == "11011.aa39d7a3e"
    assert result["image_id"] == "sha256:image-a"
    assert result["request_settings_observed"] is False
    assert "verification" not in result
    assert commands[1][1:3] == ["exec", "container-a"]
    assert all("start" not in command and "restart" not in command for command in commands)


@pytest.mark.parametrize("fault", ["idle", "unowned", "missing", "generation", "runtime"])
def test_unavailable_identity_does_not_execute_or_wake(managed, fault):
    manager, spec, state, commands = managed
    if fault == "idle":
        state.running = False
    elif fault == "unowned":
        state.owned = False
    elif fault == "missing":
        state.exists = False
    else:
        state.labels[f"com.dotori.{fault}"] = "different"
    assert manager.observe_backend(spec)["status"] == "unavailable"
    assert commands == []


def test_changed_container_is_rejected(managed):
    manager, spec, _, _ = managed
    count = 0
    original = manager._runner
    def replaced(args, **kwargs):
        nonlocal count
        count += 1
        if count == 3:
            return subprocess.CompletedProcess(args, 0, stdout="replacement|sha256:other", stderr="")
        return original(args, **kwargs)
    manager._runner = replaced
    assert manager.observe_backend(spec)["reason"] == "runtime_changed_during_observation"


def test_timeout_returns_missing_observation_without_raw_logs(managed):
    manager, spec, _, _ = managed
    def timed_out(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 15, output="private-output")
    manager._runner = timed_out
    result = manager.observe_backend(spec)
    assert result["reason"] == "backend_observation_failed"
    assert "private-output" not in str(result)


@pytest.mark.parametrize("output", ["", "error: unknown", "secret\n0.11.0", "version: unknown (unknown)"])
def test_unrecognized_output_cannot_be_promoted(output):
    assert parse_backend_version("llama.cpp", output) is None


def test_vllm_metadata_probe_does_not_import_model_runtime(managed):
    manager, spec, state, commands = managed
    spec = build_runtime_spec("production", "vllm", "test", "generation-a", repo_root=manager.repo_root)
    state.labels["com.dotori.runtime"] = "vllm"
    original = manager._runner
    def runner(args, **kwargs):
        if args[1] == "exec":
            commands.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="0.11.0", stderr="")
        return original(args, **kwargs)
    manager._runner = runner
    assert manager.observe_backend(spec)["backend_version"] == "0.11.0"
    assert "importlib.metadata" in commands[1][-1]
    assert "import vllm" not in commands[1][-1]


def test_version_format_is_not_filled_from_catalog_evidence():
    assert parse_backend_version("llama.cpp", "version: 11011 (aa39d7a3e)") != "0.4.1-dev+11011.aa39d7a3e"


def test_llama_version_on_stderr_is_supported(managed):
    manager, spec, _, _ = managed
    original = manager._runner
    def stderr_version(args, **kwargs):
        if args[1] == "exec":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="version: 11011 (aa39d7a3e)\n")
        return original(args, **kwargs)
    manager._runner = stderr_version
    assert manager.observe_backend(spec)["backend_version"] == "11011.aa39d7a3e"


def test_direct_reported_version_is_preserved():
    assert parse_backend_version("llama.cpp", "version: 0.4.1-dev+11011.aa39d7a3e") == "0.4.1-dev+11011.aa39d7a3e"


def test_active_observer_reads_selection_without_leaking_other_settings(managed, monkeypatch):
    from llm_installation.backend_observation import observe_active_llm_backend
    manager, spec, _, _ = managed
    monkeypatch.setattr("llm_installation.rag_runtime_config.load_llm_runtime_config", lambda **kw: {
        "target": {"runtime": spec.runtime, "model": spec.model_id, "generation_id": spec.generation_id,
                   "api_key": "private-api-key"}})
    result = observe_active_llm_backend(manager)
    assert result["status"] == "backend_observed"
    assert "private-api-key" not in str(result)
    assert "api_key" not in result


def test_active_observer_requires_configured_selection(managed, monkeypatch):
    from llm_installation.backend_observation import observe_active_llm_backend
    manager, _, _, commands = managed
    monkeypatch.setattr("llm_installation.rag_runtime_config.load_llm_runtime_config", lambda **kw: {})
    assert observe_active_llm_backend(manager)["reason"] == "active_runtime_not_configured"
    assert commands == []


def test_failed_version_command_returns_unavailable(managed):
    manager, spec, _, _ = managed
    original = manager._runner
    def failure(args, **kwargs):
        if args[1] == "exec":
            return subprocess.CompletedProcess(args, 1, stdout="0.11.0", stderr="private-error")
        return original(args, **kwargs)
    manager._runner = failure
    result = manager.observe_backend(spec)
    assert result["reason"] == "backend_version_unavailable"
    assert "private-error" not in str(result)


def test_actual_llama_semantic_build_output_is_normalized_from_reported_facts():
    output = "version: 0.4.1-dev (build 11011, commit aa39d7a3e)\nbuilt with GNU 14.2.0 for Linux x86_64\n"
    assert parse_backend_version("llama.cpp", output) == "0.4.1-dev+11011.aa39d7a3e"
