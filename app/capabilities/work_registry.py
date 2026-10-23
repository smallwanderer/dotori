"""Model-free contracts for durable work shipped with the current modules."""

from dataclasses import dataclass
from types import MappingProxyType

from capabilities.registry import DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH


@dataclass(frozen=True)
class WorkDefinition:
    kind: str
    capability_id: str
    execution_role: str
    queue: str
    task_name: str
    argument_fields: tuple[str, ...]
    handler_path: str | None = None
    plan_adapter_path: str | None = None

    def deployment_entry(self) -> dict[str, str]:
        return {
            "capability_id": self.capability_id,
            "execution_role": self.execution_role,
            "queue": self.queue,
            "task_name": self.task_name,
            "argument_fields": list(self.argument_fields),
        }


WORK_REGISTRY = MappingProxyType({
    "parse_document": WorkDefinition("parse_document", DOCUMENT_EXTRACT, "parser", "parse", "documents.tasks.parse_document_with_docling", ("node_id",), "documents.processing.work_handlers.PARSE_HANDLER", "documents.services.plan_delivery.DOMAIN_ADAPTER"),
    "embed_document": WorkDefinition("embed_document", DOCUMENT_SEMANTIC_SEARCH, "embedding", "embed", "documents.tasks.embed_document_scoped", ("schema", "command", "manifest"), "documents.processing.work_handlers.SCOPED_EMBEDDING_HANDLER", "documents.processing.embedding_plan_adapter.EMBEDDING_DOMAIN_ADAPTER"),
    "plan_document_embedding": WorkDefinition("plan_document_embedding", DOCUMENT_SEMANTIC_SEARCH, "embedding", "embed", "documents.tasks.enqueue_embedding_tasks", ("node_id",), "documents.processing.work_handlers.PLAN_EMBEDDING_HANDLER"),
    "embed_document_batch": WorkDefinition("embed_document_batch", DOCUMENT_SEMANTIC_SEARCH, "embedding", "embed", "documents.tasks.embedding_document_batch_with_bge", ("chunk_ids",), "documents.processing.work_handlers.EMBED_BATCH_HANDLER"),
    "evaluate_retrieval": WorkDefinition("evaluate_retrieval", DOCUMENT_SEMANTIC_SEARCH, "embedding", "embed", "documents.tasks.run_quality_evaluation_task", ("run_uid",), "documents.processing.work_handlers.EVALUATION_HANDLER"),
})


def get_work_definition(kind: str) -> WorkDefinition:
    try:
        return WORK_REGISTRY[kind]
    except KeyError as exc:
        raise ValueError(f"Unknown work kind: {kind}") from exc


def get_preparation_work_definition(kind):
    """Plan-only contracts from existing composition declarations, not routes."""
    if kind in WORK_REGISTRY:
        return get_work_definition(kind)
    from capabilities.functions import COMPOSITION_DEFINITIONS
    composition = next((item for item in COMPOSITION_DEFINITIONS.values() if item.work_kind == kind), None)
    if composition is None:
        raise ValueError(f"Unknown preparation work: {kind}")
    return WorkDefinition(kind, composition.capability_id, composition.preferred_role,
        "", "", (), plan_adapter_path=composition.plan_adapter_path)


def require_registered_work(kind: str) -> WorkDefinition:
    """Verify that an installed deployment advertises this exact work route."""
    from capabilities.deployment import load_deployment_snapshot
    from capabilities.execution import require_registered_role

    definition = get_work_definition(kind)
    snapshot = load_deployment_snapshot()
    if snapshot.work_routes.get(kind) != definition.deployment_entry():
        raise ValueError(f"Work kind {kind} is not registered in the active deployment")
    if not snapshot.is_enabled(definition.capability_id):
        raise ValueError(f"Capability {definition.capability_id} is disabled")
    require_registered_role(definition.execution_role)
    return definition
