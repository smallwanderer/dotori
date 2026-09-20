"""Static, model-free declarations for capabilities shipped with Dotori."""

from dataclasses import dataclass
from types import MappingProxyType

DOCUMENT_EXTRACT = "documents.extract"
DOCUMENT_SEMANTIC_SEARCH = "documents.semantic_search"


@dataclass(frozen=True)
class CapabilityDefinition:
    capability_id: str
    contract_version: int
    dependencies: tuple[str, ...]
    input_kinds: tuple[str, ...]
    output_schema: str
    execution_roles: tuple[str, ...]


CAPABILITY_REGISTRY = MappingProxyType({
    DOCUMENT_EXTRACT: CapabilityDefinition(DOCUMENT_EXTRACT, 1, (), ("document",), "dotori.document.extract.v1", ("parser",)),
    DOCUMENT_SEMANTIC_SEARCH: CapabilityDefinition(DOCUMENT_SEMANTIC_SEARCH, 1, (DOCUMENT_EXTRACT,), ("document",), "dotori.document.embedding.v1", ("embedding",)),
})


def get_capability(capability_id: str) -> CapabilityDefinition:
    try:
        return CAPABILITY_REGISTRY[capability_id]
    except KeyError as exc:
        raise ValueError(f"Unknown capability: {capability_id}") from exc


def resolve_capabilities(capability_ids: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(capability_ids))
    unknown = sorted(set(requested) - set(CAPABILITY_REGISTRY))
    if unknown:
        raise ValueError(f"Unknown capabilities: {', '.join(unknown)}")
    resolved: list[str] = []
    visiting: set[str] = set()

    def visit(capability_id: str) -> None:
        if capability_id in resolved:
            return
        if capability_id in visiting:
            raise ValueError(f"Cyclic capability dependency: {capability_id}")
        visiting.add(capability_id)
        for dependency in get_capability(capability_id).dependencies:
            if dependency not in requested:
                raise ValueError(f"Capability {capability_id} requires {dependency}")
            visit(dependency)
        visiting.remove(capability_id)
        resolved.append(capability_id)

    for capability_id in requested:
        visit(capability_id)
    return tuple(resolved)
