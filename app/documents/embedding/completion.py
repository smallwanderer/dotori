"""Domain result/exception interpretation over the shared receipt lifecycle.

Internal port only: delivery authorization and route activation remain separate.
"""
from documents.models import ExecutorJobReceipt
from documents.embedding.scoped_execution import execute_scoped_embedding
from documents.services.executor_jobs import finish_execution, fail_execution
from orchestration.receipts import active_receipt_ownership, ExecutionOwnershipLost
from orchestration.execution import StaleExecution


def execute_scoped_receipted(envelope):
    owner = active_receipt_ownership.get()
    if owner is None or owner.job_id != envelope["job_id"]:
        raise ExecutionOwnershipLost("Completion requires the active receipt owner")
    receipt = ExecutorJobReceipt.objects.get(pk=owner.job_id)
    owner.validate(receipt)
    try:
        if receipt.status == "succeeded":
            execute_scoped_embedding(envelope)  # Identity checks and replay, no inference.
            return receipt
        return execute_scoped_embedding(envelope,
            on_completed=lambda result: _commit_result(receipt, result))
    except ExecutionOwnershipLost:
        raise  # Never record an old attempt's failure on the current owner.
    except Exception as exc:
        if receipt.status != "running":
            raise
        retryable = not isinstance(exc, (ValueError, FileNotFoundError))
        code = "STALE_INPUT" if isinstance(exc, StaleExecution) else getattr(exc, "code", "EXECUTION_FAILED")
        return fail_execution(receipt, code=code,
            message=str(exc), retryable=retryable)


def _commit_result(receipt, result):
    if receipt.status == "succeeded":
        return receipt
    if result["ready"]:
        # Called inside scoped execution's final validated transaction.
        return finish_execution(receipt, result=result)
    # Current non-ready terminal outcomes are empty-input failures, not OOM.
    # Transient inference errors take the exception path above.
    return fail_execution(receipt, code="EMBEDDING_INCOMPLETE",
        message="Approved embedding scope is incomplete", retryable=False, result=result)
