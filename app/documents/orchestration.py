"""Transport facade and routing contract for asynchronous AI work.

Domain callers use these entry points instead of importing Celery tasks.  The
route table is deliberately small and transport-neutral at the call site; the
current adapter remains Celery so task names, acknowledgements and retries stay
compatible with existing workers. Shared delivery implementation lives in
the top-level orchestration package; this facade owns document task wrappers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from orchestration.contracts import TASK_ENVELOPE_VERSION, TaskEnvelope, derive_job_id
from orchestration.dispatch import dispatch_task, publish_registered_work
from orchestration.backpressure import broker_redis_client, queue_backpressure
from capabilities.work_registry import get_work_definition
from documents.services.environment import get_positive_int_env

class WorkKind(StrEnum):
    PARSE_DOCUMENT = "parse_document"
    PLAN_DOCUMENT_EMBEDDING = "plan_document_embedding"
    EMBED_DOCUMENT_BATCH = "embed_document_batch"
    EVALUATE_RETRIEVAL = "evaluate_retrieval"


@dataclass(frozen=True)
class TaskRoute:
    task_name: str
    queue: str


TASK_ROUTES = MappingProxyType({
    kind: TaskRoute(get_work_definition(kind.value).task_name, get_work_definition(kind.value).queue)
    for kind in WorkKind
})

WORK_CAPABILITIES = MappingProxyType({
    kind: get_work_definition(kind.value).capability_id
    for kind in WorkKind
})


def get_task_route(kind: WorkKind) -> TaskRoute:
    definition = get_work_definition(kind.value)
    return TaskRoute(definition.task_name, definition.queue)


def get_work_capability(kind: WorkKind) -> str:
    return get_work_definition(kind.value).capability_id


enqueue_registered_work = publish_registered_work


def submit_embedding_plan(**request):
    """Explicit scoped plan intake; legacy upload producers remain unchanged."""
    from documents.processing.embedding_plan_adapter import submit_document_embedding_plan
    return submit_document_embedding_plan(**request)


def _dispatch(task, kind: WorkKind, *, args: list[Any], kwargs: dict[str, Any]):
    kwargs = dict(kwargs)
    headers = kwargs.pop("_delivery_headers", None)
    return dispatch_task(task, kind.value, args=args, kwargs=kwargs, headers=headers)


def enqueue_parse(node_id: int, **kwargs: Any):
    from documents.tasks import parse_document_with_docling

    return _dispatch(
        parse_document_with_docling,
        WorkKind.PARSE_DOCUMENT,
        args=[node_id],
        kwargs=kwargs,
    )


def enqueue_embedding(node_id: int, **kwargs: Any):
    from documents.tasks import enqueue_embedding_tasks

    return _dispatch(
        enqueue_embedding_tasks,
        WorkKind.PLAN_DOCUMENT_EMBEDDING,
        args=[node_id],
        kwargs=kwargs,
    )


def enqueue_embedding_batch(chunk_ids: list[int], **kwargs: Any):
    from documents.tasks import embedding_document_batch_with_bge

    return _dispatch(
        embedding_document_batch_with_bge,
        WorkKind.EMBED_DOCUMENT_BATCH,
        args=[chunk_ids],
        kwargs=kwargs,
    )


def enqueue_retrieval_evaluation(run_uid: str, **kwargs: Any):
    from documents.tasks import run_quality_evaluation_task

    return _dispatch(
        run_quality_evaluation_task,
        WorkKind.EVALUATE_RETRIEVAL,
        args=[run_uid],
        kwargs=kwargs,
    )



def embedding_queue_backpressure() -> tuple[bool, int | None, int]:
    """Return whether parsing should pause behind the durable embed backlog.

    This is a cross-queue orchestration policy rather than embedding business
    logic. Broker inspection deliberately fails open: Celery delivery remains
    available even when the optional backlog observation is unavailable.
    """

    limit = get_positive_int_env(
        "DOCUMENT_AI_EMBED_QUEUE_BACKPRESSURE_LIMIT",
        32,
    )
    return queue_backpressure(
        get_task_route(WorkKind.PLAN_DOCUMENT_EMBEDDING).queue,
        limit=limit, client_factory=broker_redis_client,
    )
