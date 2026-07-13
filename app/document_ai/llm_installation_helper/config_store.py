from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from document_ai.llm_installation_helper.runtime_probe import ServerRuntimeProfile


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


def _write_llama_runtime_args(config_path: Path, *, target, profile) -> Path | None:
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

    args_path = config_path.with_name("llama_rag.args")
    args_path.write_text("\n".join(args) + "\n", encoding="utf-8")
    return args_path


def _write_vllm_runtime_args(config_path: Path, *, target) -> Path | None:
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
    args_path = config_path.with_name("vllm_rag.args")
    args_path.write_text("\n".join(args) + "\n", encoding="utf-8")
    return args_path


def get_llm_runtime_config_path() -> Path:
    configured_path = os.getenv("LLM_RUNTIME_CONFIG_PATH", "").strip()
    if configured_path:
        return Path(configured_path)
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "data" / "config" / "llm_runtime.json"


def load_llm_runtime_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or get_llm_runtime_config_path()
    if not config_path.exists():
        return {}

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(payload, dict):
        return {}
    return payload


def write_llm_runtime_config(
    *,
    target,
    profile: ServerRuntimeProfile,
    catalog: list,
    path: Path | None = None,
) -> Path:
    config_path = path or get_llm_runtime_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "version": CONFIG_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": {
            "endpoint_name": target.endpoint_name,
            "base_url": target.base_url,
            "model": target.model,
            "runtime": target.runtime,
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
    config_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_llama_runtime_args(
        config_path,
        target=target,
        profile=profile,
    )
    _write_vllm_runtime_args(config_path, target=target)
    return config_path
