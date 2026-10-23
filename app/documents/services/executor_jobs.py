"""Compatibility facade for the shared receipt lifecycle and existing table."""
from orchestration.receipts import (
    ExecutorJobConflict, ExecutionClaim, canonical_fingerprint, receipt_payload,
)
from orchestration import receipts
from documents.services.receipt_repository import REPOSITORY


def claim_execution(**kwargs):
    return receipts.claim_execution(REPOSITORY, **kwargs)


def finish_execution(receipt, **kwargs):
    return receipts.finish_execution(REPOSITORY, receipt, **kwargs)


def fail_execution(receipt, **kwargs):
    return receipts.fail_execution(REPOSITORY, receipt, **kwargs)
