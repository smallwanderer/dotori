"""Observe document work while a capability deployment is draining."""

from __future__ import annotations

from dataclasses import dataclass

from document_ai.models import ExecutorJobReceipt
from document_ai.orchestration import WorkKind, get_task_route


DOCUMENT_WORK_KINDS = tuple(kind.value for kind in WorkKind)


@dataclass(frozen=True)
class DocumentDrainSnapshot:
    active_receipts: int
    parse_queue_depth: int
    embed_queue_depth: int

    @property
    def pending_work(self) -> int:
        return self.active_receipts + self.parse_queue_depth + self.embed_queue_depth


def document_drain_snapshot(*, redis_client=None) -> DocumentDrainSnapshot:
    if redis_client is None:
        from document_ai.orchestration import broker_redis_client

        redis_client = broker_redis_client()
    parse_queue = get_task_route(WorkKind.PARSE_DOCUMENT).queue
    embed_queue = get_task_route(WorkKind.PLAN_DOCUMENT_EMBEDDING).queue
    return DocumentDrainSnapshot(
        active_receipts=ExecutorJobReceipt.objects.filter(
            status=ExecutorJobReceipt.STATUS_RUNNING,
            work_kind__in=DOCUMENT_WORK_KINDS,
        ).count(),
        parse_queue_depth=int(redis_client.llen(parse_queue)),
        embed_queue_depth=int(redis_client.llen(embed_queue)),
    )
