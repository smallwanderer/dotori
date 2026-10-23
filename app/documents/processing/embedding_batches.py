"""Pure internal batch bindings, not an executable work or authorization token.

The executor owns partitioning by its count/token budget. These bindings pin
each selected subset to its parent approval; no queue/task route is added.
"""
from dataclasses import dataclass
import hashlib
import json
from uuid import UUID

from documents.processing.embedding_contract import EmbeddingContractError


def _reject(message):
    raise EmbeddingContractError("INVALID_BATCH", message)


def _ids(values):
    if (not isinstance(values, tuple) or not values
            or any(type(value) is not int or value < 1 for value in values)
            or len(set(values)) != len(values)):
        _reject("Batch targets must be distinct positive IDs")
    return tuple(sorted(values))


@dataclass(frozen=True)
class EmbeddingBatchBinding:
    parent_job_id: str
    manifest_id: str
    manifest_fingerprint: str
    target_ids: tuple[int, ...]
    batch_id: str


def bind_embedding_batch(*, parent_job_id, manifest_id, manifest_fingerprint,
                         target_ids, approved_target_ids):
    """Caller must authenticate/load the approved manifest before passing IDs.

    Batch identity is independent of retry number, order and budget tuning.
    A changed partition receives a different ID; it must still pass per-target
    ownership/attempt checks before inference. This function performs no claim.
    """
    if (not isinstance(parent_job_id, str) or not parent_job_id.strip()
            or parent_job_id != parent_job_id.strip() or len(parent_job_id) > 64):
        _reject("Invalid parent job identity")
    if not isinstance(manifest_id, str):
        _reject("Manifest ID must be canonical UUID")
    try:
        canonical = str(UUID(manifest_id))
    except ValueError:
        _reject("Manifest ID must be canonical UUID")
    if canonical != manifest_id:
        _reject("Manifest ID must be canonical UUID")
    if (not isinstance(manifest_fingerprint, str) or len(manifest_fingerprint) != 64
            or any(char not in "0123456789abcdef" for char in manifest_fingerprint)):
        _reject("Invalid manifest fingerprint")
    subset, approved = _ids(target_ids), _ids(approved_target_ids)
    if not set(subset).issubset(approved):
        _reject("Batch expands the approved manifest")
    value = {"schema": "dotori.document.embedding-batch-binding.v1",
        "parent_job_id": parent_job_id, "manifest_id": manifest_id,
        "manifest_fingerprint": manifest_fingerprint, "target_ids": subset}
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return EmbeddingBatchBinding(parent_job_id, manifest_id, manifest_fingerprint, subset, digest)


def validate_embedding_batch(binding, *, parent_job_id, manifest_id,
                             manifest_fingerprint, approved_target_ids):
    """Verify against fresh authenticated parent evidence, not client approval."""
    if not isinstance(binding, EmbeddingBatchBinding):
        _reject("Invalid batch binding")
    expected = bind_embedding_batch(parent_job_id=parent_job_id, manifest_id=manifest_id,
        manifest_fingerprint=manifest_fingerprint, target_ids=binding.target_ids,
        approved_target_ids=approved_target_ids)
    if binding != expected:
        _reject("Batch binding does not match the approved parent")
    return expected.target_ids
