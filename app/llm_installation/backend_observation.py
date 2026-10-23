"""Read-only identity/version observations of an operator-managed LLM process.

This reports backend facts, not request-option coverage or function verification.
It never starts a container or synthesizes a version from catalog evidence.
"""
import re
import subprocess


_VERSION = re.compile(r"^[0-9][A-Za-z0-9._+-]{0,127}$")
_REPORTED_VERSION = re.compile(r"^version:\s*([0-9][A-Za-z0-9._+-]{0,127})\s*$", re.MULTILINE)
_LLAMA_SEMANTIC_BUILD = re.compile(r"^version:\s*([0-9][A-Za-z0-9._+-]{0,127})\s*\(build\s+([0-9]+),\s*commit\s+([A-Za-z0-9]+)\)\s*$", re.MULTILINE)
_LLAMA_BUILD = re.compile(r"^version:\s*([0-9]+)\s*\(([A-Za-z0-9]+)\)\s*$", re.MULTILINE)


def parse_backend_version(runtime, output):
    # Accept a directly reported version, or canonical build-number.commit facts.
    value = output.strip()
    if _VERSION.fullmatch(value):
        return value
    if runtime == "llama.cpp":
        semantic = _LLAMA_SEMANTIC_BUILD.search(value)
        if semantic:
            return f"{semantic.group(1)}+{semantic.group(2)}.{semantic.group(3)}"
        reported = _REPORTED_VERSION.search(value)
        if reported:
            return reported.group(1)
        match = _LLAMA_BUILD.search(value)
        if match:
            return f"{match.group(1)}.{match.group(2)}"
    return None


def observe_llm_backend(manager, spec):
    result = {"status": "unavailable", "runtime_generation_id": spec.generation_id,
              "runtime": spec.runtime}
    state = manager.inspect(spec.scope)
    if not state.exists or not state.owned:
        return {**result, "reason": "managed_runtime_unavailable"}
    if (state.labels.get("com.dotori.generation") != spec.generation_id
            or state.labels.get("com.dotori.runtime") != spec.runtime):
        return {**result, "reason": "runtime_identity_mismatch"}
    if not state.running:
        return {**result, "reason": "runtime_not_running"}

    def run(*args):
        return manager._runner(["docker", *args], cwd=str(manager.repo_root), timeout=15)

    try:
        identity = run("inspect", "-f", "{{.Id}}|{{.Image}}", spec.container_name)
        parts = identity.stdout.strip().split("|")
        if identity.returncode or len(parts) != 2 or not all(parts):
            return {**result, "reason": "container_identity_unavailable"}
        container_id, image_id = parts
        if spec.runtime == "llama.cpp":
            command = ["/app/llama-server", "--version"]
        elif spec.runtime == "vllm":
            command = ["python3", "-c", "from importlib.metadata import version; print(version('vllm'))"]
        else:
            return {**result, "reason": "unsupported_runtime_observer"}
        response = run("exec", container_id, *command)
        version = parse_backend_version(spec.runtime, response.stdout)
        if not version and spec.runtime == "llama.cpp":
            version = parse_backend_version(spec.runtime, response.stderr)
        if response.returncode or not version:
            return {**result, "reason": "backend_version_unavailable"}
        # Pin exec to the observed ID and reject replacement during observation.
        after = run("inspect", "-f", "{{.Id}}|{{.Image}}", spec.container_name)
        current = manager.inspect(spec.scope)
        if (after.returncode or after.stdout.strip() != identity.stdout.strip()
                or not current.owned or not current.running
                or current.labels.get("com.dotori.generation") != spec.generation_id
                or current.labels.get("com.dotori.runtime") != spec.runtime):
            return {**result, "reason": "runtime_changed_during_observation"}
    except (OSError, subprocess.TimeoutExpired):
        return {**result, "reason": "backend_observation_failed"}
    return {**result, "status": "backend_observed", "observation_ref": f"llm-container:{container_id}",
            "container_id": container_id, "image_id": image_id, "backend_version": version,
            "request_settings_observed": False}


def observe_active_llm_backend(manager, scope="production"):
    from llm_installation.rag_runtime_config import load_llm_runtime_config
    from llm_installation.runtime_lifecycle import build_runtime_spec
    config = load_llm_runtime_config(scope=scope, repo_root=manager.repo_root)
    target = config.get("target") if isinstance(config, dict) else None
    if not isinstance(target, dict) or not all(target.get(key) for key in ("runtime", "model", "generation_id")):
        return {"status": "unavailable", "reason": "active_runtime_not_configured"}
    spec = build_runtime_spec(scope, target["runtime"], target["model"], target["generation_id"],
                              repo_root=manager.repo_root)
    return observe_llm_backend(manager, spec)


if __name__ == "__main__":
    import argparse
    import json
    from llm_installation.runtime_lifecycle import RuntimeLifecycleManager
    parser = argparse.ArgumentParser(description="Read the managed LLM backend version; do not wake or verify it.")
    parser.add_argument("--scope", choices=("production",), default="production")
    args = parser.parse_args()
    report = observe_active_llm_backend(RuntimeLifecycleManager(), args.scope)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "backend_observed" else 1)
