from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


def _entry_value(entry: Any, key: str):
    if isinstance(entry, Mapping):
        return entry[key]
    return getattr(entry, key)


def get_embedding_runtime_config_path(
    scope: str, *, repo_root: Path | None = None
) -> Path:
    root = repo_root or Path(__file__).resolve().parents[2]
    return (
        root
        / "data"
        / "config"
        / "runtime_scopes"
        / scope
        / "embedding_runtime.json"
    )


def build_embedding_runtime_payload(
    *, scope: str, generation_id: str, entry: Any
) -> dict:
    return {
        "schema_version": 1,
        "scope": scope,
        "catalog_id": _entry_value(entry, "id"),
        "catalog_revision": _entry_value(entry, "revision"),
        "generation_id": generation_id,
        "model_id": _entry_value(entry, "repo_id"),
        "model_revision": _entry_value(entry, "revision"),
        "tokenizer_id": _entry_value(entry, "tokenizer_id"),
        "tokenizer_revision": _entry_value(entry, "tokenizer_revision"),
        "provider": _entry_value(entry, "provider"),
        "store": _entry_value(entry, "store"),
        "dimension": _entry_value(entry, "dimension"),
        "supports_sparse": _entry_value(entry, "supports_sparse"),
        "normalize_embeddings": _entry_value(entry, "normalize_embeddings"),
        "distance_strategy": _entry_value(entry, "distance_strategy"),
        "query_prefix": _entry_value(entry, "query_prefix"),
        "document_prefix": _entry_value(entry, "document_prefix"),
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }


def write_embedding_runtime_generation(
    *,
    scope: str,
    generation_id: str,
    entry: Any,
    repo_root: Path | None = None,
) -> Path:
    active_path = get_embedding_runtime_config_path(scope, repo_root=repo_root)
    generation_dir = (
        active_path.parent / "embedding_generations" / generation_id
    )
    generation_dir.mkdir(parents=True, exist_ok=True)
    payload = build_embedding_runtime_payload(
        scope=scope, generation_id=generation_id, entry=entry
    )
    # Validate and populate the fingerprint without importing document_ai.
    unsigned = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    import hashlib

    payload["runtime_fingerprint"] = hashlib.sha256(
        unsigned.encode("utf-8")
    ).hexdigest()
    (generation_dir / "runtime.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return generation_dir


def commit_active_embedding_runtime(
    scope: str,
    generation_id: str,
    *,
    repo_root: Path | None = None,
) -> Path:
    active_path = get_embedding_runtime_config_path(scope, repo_root=repo_root)
    generation_path = (
        active_path.parent
        / "embedding_generations"
        / generation_id
        / "runtime.json"
    )
    payload_text = generation_path.read_text(encoding="utf-8")
    active_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = active_path.with_suffix(".json.tmp")
    tmp_path.write_text(payload_text, encoding="utf-8")
    os.replace(tmp_path, active_path)
    return active_path
