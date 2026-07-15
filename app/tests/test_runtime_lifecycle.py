import json
import subprocess

import pytest

from llm_installation.runtime_lifecycle import (
    RuntimeLifecycleManager,
    build_runtime_spec,
)

pytestmark = pytest.mark.unit


def _spec(tmp_path, scope="production", runtime="llama.cpp", generation_id="gen1", model_id="test-model"):
    generation_dir = (
        tmp_path / "data" / "config" / "runtime_scopes" / scope / "generations" / generation_id
    )
    generation_dir.mkdir(parents=True)
    (generation_dir / "runtime.args").write_text("--hf-repo test/repo\n", encoding="utf-8")
    (generation_dir / "runtime.json").write_text(
        json.dumps(
            {"target": {"runtime": runtime, "model": model_id, "generation_id": generation_id}}
        ),
        encoding="utf-8",
    )
    return build_runtime_spec(scope, runtime, model_id, generation_id, repo_root=tmp_path)


class FakeDockerEnvironment:
    """Minimal in-memory Docker simulator injected as RuntimeLifecycleManager's
    runner, so apply()/stop()/remove()/status() can be exercised without a
    real Docker daemon."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.networks: set[str] = set()
        self.images: set[str] = set()
        self.containers: dict[str, dict] = {}
        self.health_after_start = "healthy"

    def seed_container(self, name, *, labels, running=True, health="healthy"):
        self.containers[name] = {"labels": labels, "running": running, "health": health}

    def __call__(self, args, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(args)
        assert args[0] == "docker"
        sub = args[1]

        if sub == "network":
            name = args[3]
            if args[2] == "inspect":
                return self._ok() if name in self.networks else self._fail()
            if args[2] == "create":
                self.networks.add(name)
                return self._ok()

        if sub == "build":
            # docker build -q -t <image> <context>
            image = args[4]
            self.images.add(image)
            return self._ok(stdout="sha256:fakeimageid")

        if sub == "inspect":
            # docker inspect -f <fmt> <target>
            fmt, target = args[3], args[4]
            if target in self.images:
                return self._ok(stdout="sha256:fakeimageid")
            container = self.containers.get(target)
            if container is None:
                return self._fail()
            if fmt == "{{json .Config.Labels}}":
                return self._ok(stdout=json.dumps(container["labels"]))
            if fmt == "{{.State.Health.Status}}":
                return self._ok(stdout=container["health"] or "")
            running = "true" if container["running"] else "false"
            return self._ok(stdout=f"{running}|{container['health'] or ''}")

        if sub == "run":
            name = args[args.index("--name") + 1]
            labels = {}
            for i, a in enumerate(args):
                if a == "--label":
                    key, _, value = args[i + 1].partition("=")
                    labels[key] = value
            self.containers[name] = {
                "labels": labels,
                "running": True,
                "health": self.health_after_start,
            }
            return self._ok()

        if sub == "rm":
            self.containers.pop(args[-1], None)
            return self._ok()

        if sub == "rename":
            old_name, new_name = args[2], args[3]
            if old_name not in self.containers:
                return self._fail()
            self.containers[new_name] = self.containers.pop(old_name)
            return self._ok()

        if sub == "stop":
            container = self.containers.get(args[-1])
            if container is not None:
                container["running"] = False
            return self._ok()

        if sub == "start":
            container = self.containers.get(args[-1])
            if container is None:
                return self._fail()
            container["running"] = True
            container["health"] = "healthy"
            return self._ok()

        if sub == "exec":
            return self._ok()

        if sub == "compose":
            return self._ok()

        raise AssertionError(f"Unhandled fake docker call: {args}")

    @staticmethod
    def _ok(stdout: str = "") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    @staticmethod
    def _fail(stderr: str = "not found") -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def test_apply_fresh_install_starts_and_commits(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.apply(spec)

    assert result.ok is True
    assert env.containers[spec.container_name]["running"] is True
    run_call = next(c for c in env.calls if c[1] == "run")
    assert "--network-alias" in run_call
    assert "rag-runtime" in run_call
    assert f"com.dotori.scope={spec.scope}" in run_call

    active_path = tmp_path / "data" / "config" / "runtime_scopes" / "production" / "llm_runtime.json"
    assert active_path.exists()
    committed = json.loads(active_path.read_text(encoding="utf-8"))
    assert committed["target"]["generation_id"] == spec.generation_id


def test_apply_refuses_name_collision_with_foreign_container(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.seed_container(spec.container_name, labels={"some.other.tool": "true"}, running=True)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.apply(spec)

    assert result.ok is False
    assert "label mismatch" in result.messages[0]
    assert spec.container_name in env.containers
    assert not any(c[1] == "run" for c in env.calls)


def test_apply_rolls_back_on_unhealthy_candidate(tmp_path):
    env = FakeDockerEnvironment()
    owned_labels = {
        "com.dotori.managed": "true",
        "com.dotori.component": "rag-runtime",
        "com.dotori.scope": "production",
        "com.dotori.runtime": "llama.cpp",
        "com.dotori.generation": "old-gen",
    }
    spec = _spec(tmp_path, generation_id="new-gen")
    env.seed_container(spec.container_name, labels=owned_labels, running=True, health="healthy")
    env.health_after_start = "unhealthy"
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    result = manager.apply(spec)

    assert result.ok is False
    assert result.rolled_back is True
    assert env.containers[spec.container_name]["running"] is True
    active_path = tmp_path / "data" / "config" / "runtime_scopes" / "production" / "llm_runtime.json"
    assert not active_path.exists()


def test_stop_refuses_when_container_not_owned(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.seed_container(spec.container_name, labels={}, running=True)
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    assert manager.stop("production") is False
    assert spec.container_name in env.containers


def test_stop_removes_owned_container(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.seed_container(
        spec.container_name,
        labels={
            "com.dotori.managed": "true",
            "com.dotori.component": "rag-runtime",
            "com.dotori.scope": "production",
        },
        running=True,
    )
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    assert manager.stop("production") is True
    assert spec.container_name not in env.containers


def test_status_reports_running_state(tmp_path):
    env = FakeDockerEnvironment()
    spec = _spec(tmp_path)
    env.seed_container(
        spec.container_name,
        labels={
            "com.dotori.managed": "true",
            "com.dotori.component": "rag-runtime",
            "com.dotori.scope": "production",
            "com.dotori.runtime": "vllm",
            "com.dotori.generation": "gen42",
        },
        running=True,
        health="healthy",
    )
    manager = RuntimeLifecycleManager(tmp_path, runner=env)

    status = manager.status("production")

    assert status["running"] is True
    assert status["owned"] is True
    assert status["runtime"] == "vllm"
    assert status["generation"] == "gen42"
