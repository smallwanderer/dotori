"""Static installation contracts for modules shipped with Dotori Core."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from capabilities.registry import DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH

DOCUMENTS_MODULE = "documents"


@dataclass(frozen=True)
class ProviderRequirement:
    role: str
    provider_id: str
    config_source: str
    default_enabled: bool = True


@dataclass(frozen=True)
class ModuleDefinition:
    module_id: str
    version: int
    capabilities: tuple[str, ...]
    dependencies: tuple[str, ...]
    providers: tuple[ProviderRequirement, ...]
    work_kinds: tuple[str, ...]

    @property
    def execution_roles(self) -> tuple[str, ...]:
        return tuple(provider.role for provider in self.providers)


MODULE_REGISTRY = MappingProxyType(
    {
        DOCUMENTS_MODULE: ModuleDefinition(
            module_id=DOCUMENTS_MODULE,
            version=1,
            capabilities=(DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH),
            dependencies=(),
            providers=(
                ProviderRequirement("parser", "docling", "DOCUMENT_PARSER_BACKEND"),
                ProviderRequirement("embedding", "active-embedding", "embedding_runtime.json"),
                ProviderRequirement("llm", "active-llm", "llm_runtime.json"),
            ),
            work_kinds=(
                "embed_document",
                "parse_document",
                "plan_document_embedding",
                "embed_document_batch",
                "evaluate_retrieval",
            ),
        )
    }
)


def get_module(module_id: str) -> ModuleDefinition:
    try:
        return MODULE_REGISTRY[module_id]
    except KeyError as exc:
        raise ValueError(f"Unknown module: {module_id}") from exc


def resolve_modules(module_ids: list[str] | tuple[str, ...]) -> tuple[ModuleDefinition, ...]:
    """Resolve dependencies in stable dependency-first order."""
    requested = tuple(dict.fromkeys(module_ids))
    unknown = sorted(set(requested) - set(MODULE_REGISTRY))
    if unknown:
        raise ValueError(f"Unknown modules: {', '.join(unknown)}")

    resolved: list[ModuleDefinition] = []
    resolved_ids: set[str] = set()
    visiting: set[str] = set()

    def visit(module_id: str) -> None:
        if module_id in resolved_ids:
            return
        if module_id in visiting:
            raise ValueError(f"Cyclic module dependency: {module_id}")
        visiting.add(module_id)
        module = get_module(module_id)
        for dependency in module.dependencies:
            if dependency not in MODULE_REGISTRY:
                raise ValueError(
                    f"Module {module_id} requires unknown module {dependency}"
                )
            visit(dependency)
        visiting.remove(module_id)
        resolved.append(module)
        resolved_ids.add(module_id)

    for module_id in requested:
        visit(module_id)
    return tuple(resolved)
