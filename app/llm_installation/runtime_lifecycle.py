from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

RUNTIME_IMAGE = {
    "llama.cpp": "dotori/llama-rag",
    "vllm": "dotori/vllm-rag",
}

RUNTIME_BUILD_CONTEXT = {
    "llama.cpp": "llama-rag",
    "vllm": "vllm-rag",
}

RUNTIME_ARGS_ENV = {
    "llama.cpp": "LLAMA_RAG_ARGS_FILE",
    "vllm": "VLLM_RAG_ARGS_FILE",
}

NETWORK_ALIAS = "rag-runtime"

LABEL_MANAGED = "com.dotori.managed"
LABEL_COMPONENT = "com.dotori.component"
LABEL_SCOPE = "com.dotori.scope"
LABEL_RUNTIME = "com.dotori.runtime"
LABEL_GENERATION = "com.dotori.generation"
LABEL_IMAGE_REVISION = "com.dotori.image-revision"
COMPONENT_VALUE = "rag-runtime"

HEALTH_POLL_INTERVAL_S = 2
HEALTH_TIMEOUT_S = {"llama.cpp": 180, "vllm": 240}


@dataclass(frozen=True)
class ScopeConfig:
    compose_file: str
    env_file: str
    network_name: str
    container_name: str


SCOPE_CONFIG = {
    "production": ScopeConfig(
        compose_file="docker-compose.yml",
        env_file=".env",
        network_name="dotori-runtime",
        container_name="dotori-rag-runtime",
    ),
    "development": ScopeConfig(
        compose_file="docker-compose.dev.yml",
        env_file=".env.dev",
        network_name="dotori-dev-runtime",
        container_name="dotori-dev-rag-runtime",
    ),
}


@dataclass(frozen=True)
class RuntimeSpec:
    scope: str
    runtime: str
    model_id: str
    image: str
    container_name: str
    network_name: str
    network_alias: str
    generation_id: str
    args_file: Path
    health_url: str
    model_probe_url: str | None


@dataclass
class ApplyResult:
    ok: bool
    rolled_back: bool = False
    messages: list[str] = field(default_factory=list)


@dataclass
class ContainerState:
    exists: bool
    owned: bool = False
    running: bool = False
    health: str | None = None
    labels: dict[str, str] = field(default_factory=dict)


def get_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def make_generation_id(integrity_sha256: str) -> str:
    return f"{int(time.time())}-{integrity_sha256[:12]}"


def build_runtime_spec(
    scope: str,
    runtime: str,
    model_id: str,
    generation_id: str,
    *,
    repo_root: Path | None = None,
) -> RuntimeSpec:
    if scope not in SCOPE_CONFIG:
        raise ValueError(f"Unknown scope: {scope}")
    if runtime not in RUNTIME_IMAGE:
        raise ValueError(f"Unknown runtime: {runtime}")

    root = repo_root or get_repo_root()
    scope_cfg = SCOPE_CONFIG[scope]
    generation_dir = (
        root / "data" / "config" / "runtime_scopes" / scope / "generations" / generation_id
    )
    return RuntimeSpec(
        scope=scope,
        runtime=runtime,
        model_id=model_id,
        image=RUNTIME_IMAGE[runtime],
        container_name=scope_cfg.container_name,
        network_name=scope_cfg.network_name,
        network_alias=NETWORK_ALIAS,
        generation_id=generation_id,
        args_file=generation_dir / "runtime.args",
        health_url="http://localhost:8080/health",
        model_probe_url="http://localhost:8080/v1/models",
    )


def _default_runner(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)
    except OSError as exc:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=str(exc))


class RuntimeLifecycleManager:
    """Owns the Docker lifecycle of the single, scope-scoped RAG runtime
    container. Never selects a model/backend/runtime itself -- it only
    applies an already-resolved RuntimeSpec. See
    dev-docs/agent/rag_runtime_container_lifecycle.md for the full contract.
    """

    def __init__(
        self,
        repo_root: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ):
        self.repo_root = repo_root or get_repo_root()
        self._runner = runner or _default_runner

    def _docker(self, *args: str) -> subprocess.CompletedProcess:
        return self._runner(["docker", *args], cwd=str(self.repo_root))

    def _compose(self, scope: str, *args: str) -> subprocess.CompletedProcess:
        compose_file = SCOPE_CONFIG[scope].compose_file
        return self._runner(
            ["docker", "compose", "-f", compose_file, *args], cwd=str(self.repo_root)
        )

    # -- inspection -----------------------------------------------------

    def ensure_network(self, scope: str) -> bool:
        network_name = SCOPE_CONFIG[scope].network_name
        result = self._docker("network", "inspect", network_name)
        if result.returncode == 0:
            return True
        result = self._docker("network", "create", network_name)
        return result.returncode == 0

    def inspect(self, scope: str) -> ContainerState:
        container_name = SCOPE_CONFIG[scope].container_name
        result = self._docker(
            "inspect",
            "-f",
            "{{.State.Running}}|{{.State.Health.Status}}",
            container_name,
        )
        if result.returncode != 0:
            return ContainerState(exists=False)

        labels = self._container_labels(container_name)
        owned = (
            labels.get(LABEL_MANAGED) == "true"
            and labels.get(LABEL_COMPONENT) == COMPONENT_VALUE
            and labels.get(LABEL_SCOPE) == scope
        )
        parts = result.stdout.strip().split("|")
        running = parts[0] == "true" if parts else False
        health = parts[1] if len(parts) > 1 and parts[1] else None
        return ContainerState(
            exists=True, owned=owned, running=running, health=health, labels=labels
        )

    def _container_labels(self, container_name: str) -> dict[str, str]:
        result = self._docker(
            "inspect", "-f", "{{json .Config.Labels}}", container_name
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        import json as _json

        try:
            return _json.loads(result.stdout.strip()) or {}
        except ValueError:
            return {}

    # -- build/run --------------------------------------------------------

    def build(self, spec: RuntimeSpec) -> tuple[bool, str | None]:
        build_context = self.repo_root / RUNTIME_BUILD_CONTEXT[spec.runtime]
        result = self._docker("build", "-q", "-t", spec.image, str(build_context))
        if result.returncode != 0:
            return False, None
        revision = result.stdout.strip() or None
        if not revision:
            id_result = self._docker("inspect", "-f", "{{.Id}}", spec.image)
            revision = id_result.stdout.strip() if id_result.returncode == 0 else None
        return True, revision

    def _run_candidate(self, spec: RuntimeSpec, image_revision: str | None) -> bool:
        self._docker("rm", "-f", spec.container_name)

        cache_dir = self.repo_root / "data" / "cache" / "huggingface"
        cache_dir.mkdir(parents=True, exist_ok=True)

        run_args = [
            "run", "-d",
            "--name", spec.container_name,
            "--network", spec.network_name,
            "--network-alias", spec.network_alias,
            "--restart", "unless-stopped",
            "--env-file", str(self.repo_root / SCOPE_CONFIG[spec.scope].env_file),
            "-v", f"{cache_dir}:/root/.cache/huggingface",
            "-v", f"{spec.args_file}:/runtime/runtime.args:ro",
            "-e", f"{RUNTIME_ARGS_ENV[spec.runtime]}=/runtime/runtime.args",
            "--label", f"{LABEL_MANAGED}=true",
            "--label", f"{LABEL_COMPONENT}={COMPONENT_VALUE}",
            "--label", f"{LABEL_SCOPE}={spec.scope}",
            "--label", f"{LABEL_RUNTIME}={spec.runtime}",
            "--label", f"{LABEL_GENERATION}={spec.generation_id}",
        ]
        if image_revision:
            run_args += ["--label", f"{LABEL_IMAGE_REVISION}={image_revision}"]

        if spec.runtime == "vllm":
            run_args += [
                "--gpus", "all",
                "--shm-size", "4g",
                "--health-cmd",
                "python3 -c \"import urllib.request; "
                "urllib.request.urlopen('http://localhost:8080/health', timeout=2)\"",
                "--health-interval", "10s",
                "--health-timeout", "5s",
                "--health-retries", "12",
                "--health-start-period", "180s",
            ]
        else:
            run_args += [
                "--health-cmd", "curl -f http://localhost:8080/health",
                "--health-interval", "10s",
                "--health-timeout", "5s",
                "--health-retries", "12",
                "--health-start-period", "120s",
            ]
        run_args.append(spec.image)

        result = self._docker(*run_args)
        return result.returncode == 0

    def _wait_healthy(self, spec: RuntimeSpec) -> bool:
        timeout = HEALTH_TIMEOUT_S.get(spec.runtime, 180)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._docker(
                "inspect", "-f", "{{.State.Health.Status}}", spec.container_name
            )
            status = result.stdout.strip() if result.returncode == 0 else ""
            if status == "healthy":
                return True
            if status == "unhealthy":
                return False
            time.sleep(HEALTH_POLL_INTERVAL_S)
        return False

    def _verify_endpoints(self, spec: RuntimeSpec) -> tuple[bool, str]:
        health = self._docker(
            "exec", spec.container_name, "curl", "-sf", spec.health_url
        )
        if health.returncode != 0:
            return False, f"/health check failed: {health.stderr.strip()[:200]}"
        if spec.model_probe_url:
            probe = self._docker(
                "exec", spec.container_name, "curl", "-sf", spec.model_probe_url
            )
            if probe.returncode != 0:
                return True, "/v1/models probe failed (non-fatal)"
        return True, "ok"

    # -- lifecycle --------------------------------------------------------

    def apply(self, spec: RuntimeSpec) -> ApplyResult:
        from llm_installation.config_store import commit_active_runtime_config

        messages: list[str] = []
        if not spec.args_file.exists():
            return ApplyResult(ok=False, messages=[f"Missing args file: {spec.args_file}"])

        self.ensure_network(spec.scope)

        built, revision = self.build(spec)
        if not built:
            return ApplyResult(ok=False, messages=["Failed to build runtime image."])

        self._compose(spec.scope, "stop", "rag-worker")

        current = self.inspect(spec.scope)
        if current.exists and not current.owned:
            return ApplyResult(
                ok=False,
                messages=[
                    f"A container named '{spec.container_name}' exists but isn't "
                    "managed by Dotori (label mismatch); refusing to touch it."
                ],
            )

        # Docker only allows one container per name, so the previous
        # container (if any) must be renamed out of the way -- not removed --
        # before the candidate can take the name. It's only actually deleted
        # once the candidate is confirmed healthy; on failure it's renamed
        # back and restarted.
        previous_name = f"{spec.container_name}-previous"
        has_previous = False
        if current.exists and current.owned:
            self._docker("stop", spec.container_name)
            has_previous = self._docker("rename", spec.container_name, previous_name).returncode == 0

        started = self._run_candidate(spec, revision)
        healthy = started and self._wait_healthy(spec)
        verified, verify_message = (False, "container failed to start") if not started else (
            self._verify_endpoints(spec) if healthy else (False, "health check timed out")
        )

        if started and healthy and verified:
            commit_active_runtime_config(spec.scope, spec.generation_id, repo_root=self.repo_root)
            if has_previous:
                self._docker("rm", "-f", previous_name)
            self._compose(spec.scope, "start", "rag-worker")
            messages.append(f"Runtime '{spec.runtime}' active (generation {spec.generation_id}).")
            return ApplyResult(ok=True, messages=messages)

        # Rollback.
        messages.append(f"Candidate failed validation: {verify_message}")
        self._docker("rm", "-f", spec.container_name)
        rolled_back = False
        if has_previous:
            self._docker("rename", previous_name, spec.container_name)
            restart = self._docker("start", spec.container_name)
            rolled_back = restart.returncode == 0
            messages.append(
                "Restored previous runtime container."
                if rolled_back
                else "Could not restart previous runtime container; manual recovery needed."
            )
        self._compose(spec.scope, "start", "rag-worker")
        messages.append(
            f"Diagnostics: docker logs {spec.container_name} --tail 100; "
            f"generation at {spec.args_file.parent}"
        )
        return ApplyResult(ok=False, rolled_back=rolled_back, messages=messages)

    def stop(self, scope: str, remove_container: bool = True) -> bool:
        state = self.inspect(scope)
        if not state.exists:
            return True
        if not state.owned:
            return False
        container_name = SCOPE_CONFIG[scope].container_name
        self._docker("stop", container_name)
        if remove_container:
            self._docker("rm", "-f", container_name)
        return True

    def remove(self, scope: str) -> bool:
        """Stop+remove the owned container and delete the scope's whole
        config tree (active pointer + all generations). Model weight cache
        cleanup is a separate, repo-id-specific concern owned by
        llm_installation.cleanup.remove_model_weights."""
        stopped = self.stop(scope, remove_container=True)
        scope_dir = self.repo_root / "data" / "config" / "runtime_scopes" / scope
        if scope_dir.exists():
            import shutil

            shutil.rmtree(scope_dir, ignore_errors=True)
        return stopped

    def status(self, scope: str) -> dict:
        state = self.inspect(scope)
        return {
            "scope": scope,
            "container_name": SCOPE_CONFIG[scope].container_name,
            "network_name": SCOPE_CONFIG[scope].network_name,
            "exists": state.exists,
            "owned": state.owned,
            "running": state.running,
            "health": state.health,
            "runtime": state.labels.get(LABEL_RUNTIME),
            "generation": state.labels.get(LABEL_GENERATION),
        }
