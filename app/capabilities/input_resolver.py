"""Choose registered initial work from an uploaded file without loading a model."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from capabilities.registry import CAPABILITY_REGISTRY
from capabilities.work_registry import get_work_definition


DOCUMENT_EXTENSIONS = frozenset({
    ".txt", ".md", ".markdown", ".yaml", ".yml", ".json", ".py",
    ".sh", ".bash", ".sql", ".xml", ".html", ".htm", ".toml",
    ".ini", ".cfg", ".conf", ".js", ".ts", ".hwpx", ".hwp",
    ".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg",
    ".webp", ".bmp", ".tiff", ".gif",
})

# Initial work selection is explicit. Downstream embedding is planned by the
# document workflow after parsing, not published a second time on upload.
DEFAULT_WORK_BINDINGS = {"document": "parse_document"}


class InputResolutionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class InputDescriptor:
    node_id: int
    filename: str
    mime_type: str = ""


@dataclass(frozen=True)
class WorkRequest:
    kind: str
    payload: dict[str, int]


def input_kind(descriptor: InputDescriptor) -> str:
    extension = Path(descriptor.filename).suffix.lower()
    if extension in DOCUMENT_EXTENSIONS:
        return "document"
    raise InputResolutionError("UNSUPPORTED_INPUT", f"No registered input kind for {descriptor.filename}")


def resolve_work(
    input_descriptor: InputDescriptor,
    enabled_capabilities: tuple[str, ...] | list[str] | set[str],
    provider_bindings: Mapping[str, str] | None = None,
) -> tuple[WorkRequest, ...]:
    """Return one initial route; an inactive known capability produces no work."""
    kind = input_kind(input_descriptor)
    bindings = DEFAULT_WORK_BINDINGS if provider_bindings is None else provider_bindings
    work_kind = bindings.get(kind)
    if work_kind is None:
        raise InputResolutionError("NO_ACTIVE_HANDLER", f"No work route is registered for {kind} input")
    try:
        work = get_work_definition(work_kind)
        capability = CAPABILITY_REGISTRY[work.capability_id]
    except (ValueError, KeyError) as exc:
        raise InputResolutionError("WORK_NOT_REGISTERED", f"Unregistered work route for {kind} input") from exc
    if kind not in capability.input_kinds:
        raise InputResolutionError("INPUT_ROUTE_MISMATCH", f"{work_kind} does not accept {kind} input")
    if work.capability_id not in enabled_capabilities:
        return ()
    if work.argument_fields != ("node_id",):
        raise InputResolutionError("INPUT_ROUTE_MISMATCH", f"{work_kind} cannot accept a file node")
    return (WorkRequest(work_kind, {"node_id": input_descriptor.node_id}),)
