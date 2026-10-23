"""Workspace-scoped access to persisted capability results."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from config.enums import NodeType
from documents.models import DocumentParseResult
from documents.services.result_envelope import document_extract_result_envelope
from files.models import Node
from files.services.access import is_active_member


DOCUMENT_EXTRACT_RESULT_PREFIX = "document-extract:"


@dataclass(frozen=True)
class ResultAccessError(Exception):
    code: str
    message: str
    status: int

    def __str__(self) -> str:
        return self.message


def _require_workspace_access(*, workspace, membership) -> None:
    if not is_active_member(membership, workspace_id=workspace.id):
        raise ResultAccessError(
            "WORKSPACE_ACCESS_DENIED",
            "The requested workspace is unavailable or not an active membership.",
            403,
        )


def get_result_source(*, workspace, membership, source_uid) -> Node:
    _require_workspace_access(workspace=workspace, membership=membership)
    try:
        return (
            Node.objects.filter(
                workspace=workspace,
                uid=source_uid,
                node_type=NodeType.FILE,
                trashed=False,
            )
            .select_related("blob", "parse_result")
            .prefetch_related("parse_result__chunks")
            .get()
        )
    except (Node.DoesNotExist, ValueError) as exc:
        raise ResultAccessError("RESULT_SOURCE_NOT_FOUND", "Result source not found.", 404) from exc


def list_results(*, workspace, membership, source_uid=None, limit: int = 50) -> list[dict]:
    _require_workspace_access(workspace=workspace, membership=membership)
    queryset = (
        DocumentParseResult.objects.filter(
            node__workspace=workspace,
            node__trashed=False,
            node__node_type=NodeType.FILE,
        )
        .select_related("node", "node__blob")
        .prefetch_related("chunks")
        .order_by("-updated_at")
    )
    if source_uid is not None:
        try:
            normalized_source_uid = UUID(str(source_uid))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ResultAccessError(
                "INVALID_SOURCE_UID",
                "source_uid must be a valid UUID.",
                400,
            ) from exc
        queryset = queryset.filter(node__uid=normalized_source_uid)
    return [
        document_extract_result_envelope(parse_result, include_payload=False)
        for parse_result in queryset[: max(1, min(limit, 100))]
    ]


def list_source_results(*, workspace, membership, source_uid) -> list[dict]:
    get_result_source(
        workspace=workspace,
        membership=membership,
        source_uid=source_uid,
    )
    return list_results(
        workspace=workspace,
        membership=membership,
        source_uid=source_uid,
    )


def get_result(*, workspace, membership, result_id: str) -> dict:
    _require_workspace_access(workspace=workspace, membership=membership)
    if not result_id.startswith(DOCUMENT_EXTRACT_RESULT_PREFIX):
        raise ResultAccessError("RESULT_NOT_FOUND", "Result not found.", 404)
    raw_id = result_id.removeprefix(DOCUMENT_EXTRACT_RESULT_PREFIX)
    try:
        parse_result = (
            DocumentParseResult.objects.filter(
                pk=int(raw_id),
                node__workspace=workspace,
                node__trashed=False,
                node__node_type=NodeType.FILE,
            )
            .select_related("node", "node__blob")
            .prefetch_related("chunks")
            .get()
        )
    except (DocumentParseResult.DoesNotExist, TypeError, ValueError) as exc:
        raise ResultAccessError("RESULT_NOT_FOUND", "Result not found.", 404) from exc
    return document_extract_result_envelope(parse_result)


def get_document_text(*, workspace, membership, source_uid) -> str:
    results = list_source_results(
        workspace=workspace,
        membership=membership,
        source_uid=source_uid,
    )
    if not results:
        raise ResultAccessError("RESULT_NOT_FOUND", "No extraction result found.", 404)
    envelope = get_result(
        workspace=workspace,
        membership=membership,
        result_id=results[0]["id"],
    )
    return "\n\n".join(
        chunk["text"] for chunk in envelope["payload"]["chunks"] if chunk.get("text")
    )
