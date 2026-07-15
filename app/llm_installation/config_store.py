from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from document_ai.services.rag_runtime_config import get_llm_runtime_config_path
from llm_installation.runtime_probe import ServerRuntimeProfile


CONFIG_VERSION = 7


def _serialize_catalog_entry(entry) -> dict[str, Any]:
    if hasattr(entry, "model_dump"):
        return entry.model_dump(mode="json")
    return asdict(entry)


def _selected_entry_snapshot(target) -> dict[str, Any] | None:
    handoff = target.runtime_policy_input
    if not isinstance(handoff, dict):
        return None
    assessment = handoff.get("catalog_assessment")
    if not isinstance(assessment, dict):
        return None
    entry = assessment.get("catalog_entry")
    return entry if isinstance(entry, dict) else None


def _build_llama_runtime_args(*, target, profile) -> list[str] | None:
    if target.runtime != "llama.cpp" or not target.serving_profile:
        return None
    entry = _selected_entry_snapshot(target)
    if entry is None or entry.get("id") != target.model:
        return None

    plan = target.serving_profile
    hf = entry.get("hf") or {}
    artifact = entry.get("artifact") or {}
    hf_repo = str(hf.get("repo_id") or "")
    filename = artifact.get("filename")
    if filename:
        args = ["--hf-repo", hf_repo, "--hf-file", str(filename)]
    else:
        if artifact.get("quant"):
            hf_repo = f"{hf_repo}:{artifact['quant']}"
        args = ["--hf-repo", hf_repo]
    args.extend(
        [
            "--alias",
            entry["id"],
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--threads",
            str(plan.get("threads", max(1, int(profile.cpu_count or 1) - 1))),
            "--ctx-size",
            str(plan.get("server_ctx_size", plan["context_length"])),
            "--parallel",
            str(plan.get("parallel", plan["concurrency"])),
            "--batch-size",
            str(plan["batch_size"]),
            "--ubatch-size",
            str(plan["ubatch_size"]),
            "--metrics",
        ]
    )
    args.extend(
        [
            "--cache-type-k",
            str(plan.get("cache_type_k", "f16")),
            "--cache-type-v",
            str(plan.get("cache_type_v", "f16")),
            "--n-gpu-layers",
            str(plan.get("gpu_layers", plan.get("n_gpu_layers", 0))),
        ]
    )
    if plan.get("kv_cache_placement") == "ram" and plan.get("gpu_layers", 0):
        args.append("--no-kv-offload")
    return args


def _build_vllm_runtime_args(*, target) -> list[str] | None:
    if target.runtime != "vllm" or not target.serving_profile:
        return None
    entry = _selected_entry_snapshot(target)
    if entry is None or entry.get("id") != target.model:
        return None

    plan = target.serving_profile
    hf = entry.get("hf") or {}
    artifact = entry.get("artifact") or {}
    args = [
        "--model", str(hf.get("repo_id") or ""),
        "--served-model-name", entry["id"],
        "--host", "0.0.0.0",
        "--port", "8080",
        "--max-model-len", str(plan["context_length"]),
        "--max-num-seqs", str(plan["max_num_seqs"]),
        "--max-num-batched-tokens", str(plan["max_num_batched_tokens"]),
        "--tensor-parallel-size", str(plan.get("tensor_parallel_size", 1)),
        "--gpu-memory-utilization", str(plan.get("gpu_memory_utilization", 0.9)),
    ]
    if artifact.get("format") in {"awq", "gptq"}:
        args.extend(["--quantization", artifact["format"]])
    return args


def _build_runtime_args(*, target, profile: ServerRuntimeProfile) -> list[str] | None:
    if target.runtime == "llama.cpp":
        return _build_llama_runtime_args(target=target, profile=profile)
    if target.runtime == "vllm":
        return _build_vllm_runtime_args(target=target)
    return None


def _build_runtime_payload(
    *, target, profile: ServerRuntimeProfile, catalog: list, generation_id: str = ""
) -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": {
            "endpoint_name": target.endpoint_name,
            "base_url": target.base_url,
            "model": target.model,
            "runtime": target.runtime,
            "generation_id": generation_id,
            "reason": target.reason,
            "fallback_used": target.fallback_used,
            "priority_preset": target.priority_preset,
            "selection_mode": target.selection_mode,
            "selection_reason_code": target.selection_reason_code,
            "runtime_policy_input": target.runtime_policy_input,
            "serving_profile": target.serving_profile or {},
            "endpoint_status": asdict(target.endpoint_status)
            if target.endpoint_status
            else None,
            "health_status": asdict(target.health_status)
            if getattr(target, "health_status", None)
            else None,
            "smoke_status": asdict(target.smoke_status)
            if getattr(target, "smoke_status", None)
            else None,
        },
        "profile": asdict(profile),
        "catalog": [_serialize_catalog_entry(entry) for entry in catalog],
        "diagnostics": target.diagnostics or {},
    }


def write_runtime_generation(
    *,
    scope: str,
    target,
    profile: ServerRuntimeProfile,
    catalog: list,
    generation_id: str,
    repo_root: Path | None = None,
) -> Path:
    """Stage a candidate runtime config + args file under
    data/config/runtime_scopes/<scope>/generations/<generation_id>/ without
    touching the active llm_runtime.json pointer. The active pointer is only
    updated by commit_active_runtime_config, after health validation."""
    active_path = get_llm_runtime_config_path(scope, repo_root=repo_root)
    generation_dir = active_path.parent / "generations" / generation_id
    generation_dir.mkdir(parents=True, exist_ok=True)

    payload = _build_runtime_payload(
        target=target, profile=profile, catalog=catalog, generation_id=generation_id
    )
    (generation_dir / "runtime.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    args = _build_runtime_args(target=target, profile=profile)
    if args is not None:
        (generation_dir / "runtime.args").write_text(
            "\n".join(args) + "\n", encoding="utf-8"
        )

    return generation_dir


def commit_active_runtime_config(
    scope: str, generation_id: str, repo_root: Path | None = None
) -> Path:
    """Atomically point <scope>/llm_runtime.json at an already-validated
    generation's runtime.json (temp-file write + os.replace)."""
    active_path = get_llm_runtime_config_path(scope, repo_root=repo_root)
    generation_dir = active_path.parent / "generations" / generation_id
    payload_text = (generation_dir / "runtime.json").read_text(encoding="utf-8")

    active_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = active_path.with_suffix(".json.tmp")
    tmp_path.write_text(payload_text, encoding="utf-8")
    tmp_path.replace(active_path)
    return active_path


def write_llm_runtime_config(
    *,
    target,
    profile: ServerRuntimeProfile,
    catalog: list,
    path: Path | None = None,
) -> Path:
    """Write and immediately activate a runtime config at an explicit flat
    path, with no generation staging or health-gated commit. This is the
    direct-write path used by `detect_llm_runtime --write` (headless,
    operator-managed containers) and by tests that need a fixture at a known
    path; the interactive installation flow uses write_runtime_generation +
    commit_active_runtime_config instead, so a failed candidate never
    clobbers the active config."""
    config_path = path or get_llm_runtime_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    payload = _build_runtime_payload(target=target, profile=profile, catalog=catalog)
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    args = _build_runtime_args(target=target, profile=profile)
    if args is not None:
        args_filename = "llama_rag.args" if target.runtime == "llama.cpp" else "vllm_rag.args"
        config_path.with_name(args_filename).write_text(
            "\n".join(args) + "\n", encoding="utf-8"
        )
    return config_path
