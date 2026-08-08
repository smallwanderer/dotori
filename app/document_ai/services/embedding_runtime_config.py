from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EmbeddingRuntimeConfigError(RuntimeError):
    pass


class EmbeddingRuntimeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    scope: str
    catalog_id: str
    catalog_revision: str
    generation_id: str
    model_id: str
    model_revision: str
    tokenizer_id: str
    tokenizer_revision: str
    provider: str
    store: str
    dimension: int = Field(gt=0)
    supports_sparse: bool
    normalize_embeddings: bool = True
    distance_strategy: str
    query_prefix: str = ""
    document_prefix: str = ""
    runtime_fingerprint: str = ""
    resolved_at: str | None = None

    @model_validator(mode="after")
    def populate_and_validate_fingerprint(self) -> "EmbeddingRuntimeSnapshot":
        payload = self.model_dump(exclude={"runtime_fingerprint"})
        canonical = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if self.runtime_fingerprint and self.runtime_fingerprint != expected:
            raise ValueError("Embedding runtime fingerprint does not match payload.")
        self.runtime_fingerprint = expected
        return self


def get_embedding_runtime_config_path(
    scope: str | None = None, *, repo_root: Path | None = None
) -> Path:
    configured = os.getenv("EMBEDDING_RUNTIME_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured)
    resolved_scope = scope or os.getenv("RUNTIME_SCOPE", "production")
    root = repo_root or Path(__file__).resolve().parents[3]
    return (
        root
        / "data"
        / "config"
        / "runtime_scopes"
        / resolved_scope
        / "embedding_runtime.json"
    )


def _legacy_runtime_snapshot(scope: str) -> EmbeddingRuntimeSnapshot:
    """Provide the pre-runtime-file BGE default without env configurability."""
    return EmbeddingRuntimeSnapshot(
        scope=scope,
        catalog_id="legacy-env",
        catalog_revision="legacy",
        generation_id="legacy-bge-m3",
        model_id="BAAI/bge-m3",
        model_revision="legacy",
        tokenizer_id="BAAI/bge-m3",
        tokenizer_revision="legacy",
        provider="bgem3_hybrid",
        store="pgvector_chunk_1024",
        dimension=1024,
        supports_sparse=True,
        distance_strategy="inner_product",
    )


def load_embedding_runtime(
    path: Path | None = None,
    scope: str | None = None,
    *,
    repo_root: Path | None = None,
) -> EmbeddingRuntimeSnapshot:
    resolved_scope = scope or os.getenv("RUNTIME_SCOPE", "production")
    config_path = path or get_embedding_runtime_config_path(
        resolved_scope, repo_root=repo_root
    )
    if not config_path.exists():
        return _legacy_runtime_snapshot(resolved_scope)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        return EmbeddingRuntimeSnapshot.model_validate(payload)
    except Exception as exc:
        raise EmbeddingRuntimeConfigError(
            f"Invalid embedding runtime config: {config_path}"
        ) from exc


@lru_cache(maxsize=4)
def get_active_embedding_runtime(
    scope: str | None = None,
) -> EmbeddingRuntimeSnapshot:
    return load_embedding_runtime(scope=scope)


def clear_embedding_runtime_cache() -> None:
    get_active_embedding_runtime.cache_clear()
