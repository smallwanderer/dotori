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

    def deployment_entry(self) -> dict[str, str]:
        return {
            "capability_id": self.capability_id,
            "execution_role": self.execution_role,
            "queue": self.queue,
            "task_name": self.task_name,
            "argument_fields": list(self.argument_fields),
        }


WORK_REGISTRY = MappingProxyType({
    "parse_document": WorkDefinition("parse_document", DOCUMENT_EXTRACT, "parser", "parse", "document_ai.tasks.parse_document_with_docling", ("node_id",)),
    "plan_document_embedding": WorkDefinition("plan_document_embedding", DOCUMENT_SEMANTIC_SEARCH, "embedding", "embed", "document_ai.tasks.enqueue_embedding_tasks", ("node_id",)),
    "embed_document_batch": WorkDefinition("embed_document_batch", DOCUMENT_SEMANTIC_SEARCH, "embedding", "embed", "document_ai.tasks.embedding_document_batch_with_bge", ("chunk_ids",)),
    "evaluate_retrieval": WorkDefinition("evaluate_retrieval", DOCUMENT_SEMANTIC_SEARCH, "embedding", "embed", "document_ai.tasks.run_quality_evaluation_task", ("run_uid",)),
})


def get_work_definition(kind: str) -> WorkDefinition:
    try:
        return WORK_REGISTRY[kind]
    except KeyError as exc:
        raise ValueError(f"Unknown work kind: {kind}") from exc


def require_registered_work(kind: str) -> WorkDefinition:
    """Verify that an installed deployment advertises this exact work route."""
    import json

    from capabilities.config import capability_config_path, load_capability_config
    from capabilities.execution import require_registered_role

    definition = get_work_definition(kind)
    path = capability_config_path().parent / "deployment_scopes" / "production" / "deployment_plan.json"
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return definition
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Work registration is unreadable") from exc
    if not isinstance(plan, dict):
        raise ValueError("Work registration is invalid")
    routes = plan.get("work_routes")
    if routes is None:
        return definition  # Existing deployments are upgraded when the installer rewrites the plan.
    if not isinstance(routes, dict) or routes.get(kind) != definition.deployment_entry():
        raise ValueError(f"Work kind {kind} is not registered in the active deployment")
    config = load_capability_config()
    if plan.get("capability_revision") != config.revision:
        raise ValueError("Work registration revision does not match capabilities")
    if not config.is_enabled(definition.capability_id):
        raise ValueError(f"Capability {definition.capability_id} is disabled")
    require_registered_role(definition.execution_role)
    return definition
