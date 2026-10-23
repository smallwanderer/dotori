"""Receipt identity/lifecycle service over an injected storage repository."""
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from contextvars import ContextVar
from orchestration.interfaces import ReceiptRepository


class ExecutorJobConflict(ValueError):
    pass


class ExecutionOwnershipLost(ExecutorJobConflict):
    """A stale attempt must not change a newer receipt."""


@dataclass(frozen=True)
class ExecutionOwnership:
    job_id: str
    work_kind: str
    target_key: str
    payload_fingerprint: str
    attempt: int

    @classmethod
    def capture(cls, receipt):
        return cls(*(getattr(receipt, field) for field in cls.__dataclass_fields__))

    def validate(self, current):
        if self != self.capture(current):
            raise ExecutionOwnershipLost("Receipt execution ownership changed")


active_receipt_ownership = ContextVar("active_receipt_ownership", default=None)


def receipt_ownership(receipt):
    owner = active_receipt_ownership.get()
    if owner is not None:
        if owner.job_id != receipt.job_id:
            raise ExecutionOwnershipLost("Receipt does not belong to the active execution")
        return owner
    return ExecutionOwnership.capture(receipt)


@dataclass(frozen=True)
class ExecutionClaim:
    receipt: Any
    should_execute: bool


def canonical_fingerprint(payload):
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def claim_execution(repository: ReceiptRepository, *, job_id, work_kind, target_key, payload,
                    source_snapshot=None, runtime_snapshot=None):
    fingerprint = canonical_fingerprint(payload)
    with repository.claim_transaction():
        receipt = repository.locked_job(job_id)
        if receipt is not None:
            if receipt.work_kind != work_kind or receipt.payload_fingerprint != fingerprint:
                raise ExecutorJobConflict("job_id was reused with different work or payload")
            if receipt.status in {"running", "succeeded"} or not receipt.retryable:
                return ExecutionClaim(receipt, False)
            if source_snapshot is not None and receipt.source_snapshot != source_snapshot:
                raise ExecutorJobConflict("Source changed since the original attempt")
            if runtime_snapshot is not None and receipt.runtime_snapshot != runtime_snapshot:
                raise ExecutorJobConflict("Embedding runtime changed since the original attempt")
            if receipt.attempt >= 4:
                receipt = fail_execution(repository, receipt, code="RETRIES_EXHAUSTED",
                                         message="Execution attempts exhausted", retryable=False)
                return ExecutionClaim(receipt, False)
        active = repository.conflicting_job(job_id, work_kind, target_key, payload, source_snapshot)
        if active is not None:
            return ExecutionClaim(active, False)
        if receipt is not None:
            return ExecutionClaim(repository.restart(receipt), True)
        receipt, created = repository.create_claim(
            job_id=job_id, work_kind=work_kind, target_key=target_key,
            payload_fingerprint=fingerprint, source_snapshot=source_snapshot or {},
            runtime_snapshot=runtime_snapshot or {},
        )
        return ExecutionClaim(receipt, created)


def finish_execution(repository, receipt, *, result, followups=None):
    return repository.complete(receipt, result=result, followups=followups or [])


def fail_execution(repository, receipt, *, code, message, retryable, result=None):
    kwargs = {"code": code, "message": message, "retryable": retryable}
    if result is not None:
        kwargs["result"] = result
    return repository.fail(receipt, **kwargs)


def receipt_payload(receipt):
    return {field: getattr(receipt, field) for field in (
        "job_id", "work_kind", "status", "attempt", "result", "error", "retryable", "followups"
    )}
