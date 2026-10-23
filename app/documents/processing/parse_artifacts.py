"""Lightweight extraction policy and persisted-content identity.

No parser/tokenizer implementation is constructed on the request path. Declared
policy identity is separate from actual tokenizer evidence owned by executors.
"""
import hashlib
import json
from pathlib import Path


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def current_parse_policy_fingerprint():
    from capabilities.registry import DOCUMENT_SEMANTIC_SEARCH
    from documents.services.capability_admission import capability_enabled
    from documents.parsers.config import get_parser_backend
    from documents.services.embedding_runtime_config import load_embedding_runtime

    value = {"policy_version": 1, "normalization_version": 1,
             "parser_backend": get_parser_backend(), "chunking_strategy": "hierarchical"}
    if value["parser_backend"] == "docling":
        dependency_contract = Path(__file__).resolve().parents[2] / "requirements.parser-docling.txt"
        value["parser_dependency_contract"] = _digest(dependency_contract.read_text())
    if capability_enabled(DOCUMENT_SEMANTIC_SEARCH):
        from django.conf import settings
        runtime = load_embedding_runtime()
        value.update(chunking_strategy="hybrid", tokenizer=runtime.tokenizer_id,
                     tokenizer_revision=runtime.tokenizer_revision,
                     max_tokens=runtime.max_tokens,
                     token_headroom=getattr(settings, "EMBEDDING_TOKEN_HEADROOM", 128))
    return _digest(value)


def parse_content_fingerprint(chunks):
    return _digest([{key: row[key] for key in ("id", "chunk_index", "text", "chunk_meta")}
                    for row in sorted(chunks, key=lambda row: (row["chunk_index"], row["id"]))])
