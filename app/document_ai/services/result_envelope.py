"""Capability-neutral read adapter for persisted document extraction results."""

from __future__ import annotations

import hashlib
import json

from config.enums import AIStatus


DOCUMENT_EXTRACT_RESULT_SCHEMA = "dotori.document.extract.v1"


def processing_fingerprint(*, parser_name: str, parser_mode: str, metadata: dict) -> str:
    processing_config = {
        "parser_name": parser_name,
        "parser_mode": parser_mode,
        "parser_backend": metadata.get("parser_backend", ""),
        "parser_version": metadata.get("parser_version", ""),
        "chunking_strategy": metadata.get("chunking_strategy", ""),
        "tokenizer_name": metadata.get("tokenizer_name", ""),
        "chunk_max_tokens": metadata.get("chunk_max_tokens"),
    }
    canonical = json.dumps(
        processing_config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result_status(raw_status: str, *, stale: bool) -> str:
    if stale:
        return "stale"
    return {
        AIStatus.PENDING: "pending",
        AIStatus.PROCESSING: "running",
        AIStatus.COMPLETED: "ready",
        AIStatus.FAILED: "failed",
    }.get(raw_status, raw_status.lower())


def document_extract_result_envelope(parse_result) -> dict:
    """Expose current document tables through the first common result contract."""
    node = parse_result.node
    blob = node.blob
    metadata = dict(parse_result.metadata or {})
    source_blob_uuid = str(metadata.get("source_blob_uuid") or blob.uuid)
    source_checksum = str(
        metadata.get("source_checksum") or parse_result.input_document_hash or ""
    )
    current_checksum = str(blob.sha256 or "")
    stale = source_blob_uuid != str(blob.uuid) or bool(
        source_checksum and current_checksum and source_checksum != current_checksum
    )
    fingerprint = str(metadata.get("processing_fingerprint") or "")
    if not fingerprint:
        fingerprint = processing_fingerprint(
            parser_name=parse_result.parser_name,
            parser_mode=parse_result.parser_mode,
            metadata=metadata,
        )

    chunks = []
    for chunk in parse_result.chunks.all().order_by("chunk_index"):
        chunks.append(
            {
                "id": str(chunk.id),
                "index": chunk.chunk_index,
                "text": chunk.text,
                "locator": {
                    "type": "document",
                    "page_from": chunk.page_from,
                    "page_to": chunk.page_to,
                    "section": chunk.section_title,
                },
            }
        )

    return {
        "id": f"document-extract:{parse_result.id}",
        "kind": "document.extract",
        "schema": DOCUMENT_EXTRACT_RESULT_SCHEMA,
        "capability_id": "documents.extract",
        "source": {
            "id": str(node.uid),
            "revision": source_blob_uuid,
            "checksum": source_checksum,
        },
        "processing": {
            "fingerprint": fingerprint,
            "provider": parse_result.parser_name,
            "provider_version": str(metadata.get("parser_version") or ""),
        },
        "status": _result_status(parse_result.status, stale=stale),
        "stale": stale,
        "error": list(parse_result.errors or []),
        "payload": {"chunks": chunks},
        "created_at": parse_result.created_at.isoformat(),
        "updated_at": parse_result.updated_at.isoformat(),
    }
