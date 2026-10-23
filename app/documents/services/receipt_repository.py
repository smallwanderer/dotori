"""Existing receipt table and Documents-specific conflict policy."""
from contextlib import contextmanager

from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from orchestration.receipts import ExecutionOwnershipLost, receipt_ownership


class DocumentReceiptRepository:
    @property
    def model(self):
        from documents.models import ExecutorJobReceipt
        return ExecutorJobReceipt

    @contextmanager
    def claim_transaction(self):
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(72401931)")
            yield

    def locked_job(self, job_id):
        return self.model.objects.select_for_update().filter(job_id=job_id).first()

    def get(self, job_id):
        return self.model.objects.filter(job_id=job_id).first()

    def conflicting_job(self, job_id, work_kind, target_key, payload, source_snapshot):
        for active in self.model.objects.filter(status="running").exclude(job_id=job_id):
            same_target = active.work_kind == work_kind and active.target_key == target_key
            overlap = set((source_snapshot or {}).get("node_ids", [])) & set(active.source_snapshot.get("node_ids", []))
            chunks_overlap = set(payload.get("chunk_ids", [])) & set(active.source_snapshot.get("payload", {}).get("chunk_ids", []))
            if same_target or overlap or chunks_overlap:
                return active
        return None

    def restart(self, receipt):
        receipt.status = "running"
        receipt.attempt += 1
        receipt.error = {}
        receipt.retryable = False
        receipt.finished_at = None
        receipt.result = {}
        receipt.save(update_fields=["status", "attempt", "result", "error", "retryable", "finished_at", "updated_at"])
        return receipt

    def create_claim(self, **fields):
        try:
            with transaction.atomic():
                return self.model.objects.create(**fields), True
        except IntegrityError:
            return self.model.objects.select_for_update().get(
                work_kind=fields["work_kind"], target_key=fields["target_key"], status="running"
            ), False

    def complete(self, receipt, *, result, followups):
        owner = receipt_ownership(receipt)
        with transaction.atomic():
            current = self.model.objects.select_for_update().get(job_id=receipt.job_id)
            owner.validate(current)
            if current.status == "succeeded" and current.result == result and current.followups == followups:
                return current
            if current.status != "running":
                raise ExecutionOwnershipLost("Only the running owner may complete a receipt")
            current.status = "succeeded"
            current.result = result
            current.followups = followups
            current.error = {}
            current.retryable = False
            current.finished_at = timezone.now()
            current.save(update_fields=["status", "result", "followups", "error", "retryable", "finished_at", "updated_at"])
        return current

    def fail(self, receipt, *, code, message, retryable, result=None):
        owner = receipt_ownership(receipt)
        with transaction.atomic():
            current = self.model.objects.select_for_update().get(job_id=receipt.job_id)
            owner.validate(current)
            exhausted = (code == "RETRIES_EXHAUSTED" and current.status == "failed"
                         and current.retryable and current.attempt >= 4 and not retryable)
            if current.status != "running" and not exhausted:
                raise ExecutionOwnershipLost("Only the running owner may fail a receipt")
            current.status = "failed"
            current.error = {"code": code, "message": message[:1000]}
            current.retryable = retryable
            if result is not None:
                current.result = result
            current.finished_at = timezone.now()
            current.save(update_fields=["status", "result", "error", "retryable", "finished_at", "updated_at"])
        return current


REPOSITORY = DocumentReceiptRepository()
