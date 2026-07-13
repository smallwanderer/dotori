from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from document_ai.llm_installation_helper.config_store import (
    get_llm_runtime_config_path,
    load_llm_runtime_config,
)

COMPOSE_FILES = ("docker-compose.yml",)

RUNTIME_SERVICE = {
    "llama.cpp": "llama-rag",
    "vllm": "vllm-rag",
}

RUNTIME_CACHE_SUBPATH = {
    "llama.cpp": ("llama-rag", "data", "cache", "huggingface", "hub"),
    "vllm": ("vllm-rag", "data", "cache", "hub"),
}


def get_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def get_runtime_cache_dir(runtime: str, repo_root: Path | None = None) -> Path | None:
    parts = RUNTIME_CACHE_SUBPATH.get(runtime)
    if parts is None:
        return None
    return (repo_root or get_repo_root()).joinpath(*parts)


def extract_runtime_and_repo(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Pull (runtime, hf_repo_id) out of a loaded llm_runtime.json payload."""
    target = payload.get("target") if isinstance(payload, dict) else None
    if not isinstance(target, dict):
        return None
    runtime = target.get("runtime")
    handoff = target.get("runtime_policy_input")
    if not isinstance(handoff, dict):
        return None
    assessment = handoff.get("catalog_assessment")
    if not isinstance(assessment, dict):
        return None
    entry = assessment.get("catalog_entry")
    if not isinstance(entry, dict):
        return None
    repo_id = (entry.get("hf") or {}).get("repo_id")
    if not runtime or not repo_id:
        return None
    return str(runtime), str(repo_id)


def hf_cache_dirname(repo_id: str) -> str:
    return "models--" + repo_id.replace("/", "--")


def remove_model_weights(
    runtime: str, repo_id: str, repo_root: Path | None = None
) -> tuple[bool, str]:
    cache_dir = get_runtime_cache_dir(runtime, repo_root)
    if cache_dir is None:
        return False, f"Unknown runtime '{runtime}'; skipped weight cleanup."
    model_dir = cache_dir / hf_cache_dirname(repo_id)
    if not model_dir.exists():
        return False, f"No cached weights found for {repo_id} at {model_dir}."
    try:
        shutil.rmtree(model_dir)
    except OSError as exc:
        return False, (
            f"Could not remove cached weights for {repo_id} at {model_dir}: {exc}. "
            "The runtime container may still be holding a lock on these files; "
            "stop it and try again, or delete the folder manually."
        )
    return True, f"Removed cached weights for {repo_id}: {model_dir}"


def _compose_cmd(repo_root: Path, compose_file: str, *args: str) -> list[str]:
    return ["docker", "compose", "-f", str(repo_root / compose_file), *args]


def _run_compose(repo_root: Path, *args: str) -> bool:
    for compose_file in COMPOSE_FILES:
        if not (repo_root / compose_file).exists():
            continue
        try:
            result = subprocess.run(
                _compose_cmd(repo_root, compose_file, *args),
                cwd=repo_root,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            # docker CLI not installed/reachable; try the next compose file, if any.
            continue
        if result.returncode == 0:
            return True
    return False


def stop_and_remove_container(service: str, repo_root: Path | None = None) -> bool:
    root = repo_root or get_repo_root()
    return _run_compose(root, "rm", "-f", "-s", service)


def restart_service(service: str, repo_root: Path | None = None) -> bool:
    root = repo_root or get_repo_root()
    return _run_compose(root, "restart", service)


def clear_runtime_config_files(config_path: Path | None = None) -> list[Path]:
    path = config_path or get_llm_runtime_config_path()
    removed = []
    for candidate in (path, path.with_name("llama_rag.args"), path.with_name("vllm_rag.args")):
        if candidate.exists():
            candidate.unlink()
            removed.append(candidate)
    return removed


def remove_current_llm_runtime(
    *,
    remove_weights: bool = True,
    repo_root: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Stop+remove the configured runtime container, delete its cached weights,
    and clear the runtime config files."""
    root = repo_root or get_repo_root()
    path = config_path or get_llm_runtime_config_path()

    payload = load_llm_runtime_config(path)
    messages: list[str] = []
    info = extract_runtime_and_repo(payload) if payload else None

    if info:
        runtime, repo_id = info
        service = RUNTIME_SERVICE.get(runtime)
        if service:
            removed = stop_and_remove_container(service, root)
            messages.append(
                f"{'Removed' if removed else 'Could not remove (already stopped or not found)'} "
                f"container: {service}"
            )
        if remove_weights:
            _, message = remove_model_weights(runtime, repo_id, root)
            messages.append(message)
    else:
        messages.append(
            "No resolvable runtime/model in the current config; skipped container and weight cleanup."
        )

    removed_files = clear_runtime_config_files(path)
    if removed_files:
        messages.append("Cleared config files: " + ", ".join(str(p) for p in removed_files))
    else:
        messages.append("No runtime config files to clear.")

    return {
        "had_config": bool(payload),
        "messages": messages,
        "removed_config_files": [str(p) for p in removed_files],
    }


def cleanup_stale_runtime(
    previous_payload: dict[str, Any],
    new_runtime: str,
    new_repo_id: str,
    *,
    remove_weights: bool = True,
    repo_root: Path | None = None,
) -> list[str]:
    """After switching to a new runtime/model, remove leftovers from the previous selection."""
    messages: list[str] = []
    info = extract_runtime_and_repo(previous_payload) if previous_payload else None
    if info is None:
        return messages

    prev_runtime, prev_repo_id = info
    if prev_runtime == new_runtime and prev_repo_id == new_repo_id:
        return messages

    if prev_runtime != new_runtime:
        prev_service = RUNTIME_SERVICE.get(prev_runtime)
        if prev_service:
            removed = stop_and_remove_container(prev_service, repo_root)
            messages.append(
                f"{'Removed' if removed else 'Could not remove'} previous runtime container: {prev_service}"
            )

    if remove_weights and prev_repo_id != new_repo_id:
        _, message = remove_model_weights(prev_runtime, prev_repo_id, repo_root)
        messages.append(message)

    return messages
