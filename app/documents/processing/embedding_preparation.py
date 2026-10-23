"""DB-backed preparation decisions, not a publisher or enabled embedding route."""
from dataclasses import dataclass

from config.enums import AIStatus
from documents.models import DocumentParseResult
from documents.processing.embedding_contract import (
    COMMAND_SCHEMA, EmbeddingCommand, ParseBinding, RuntimeBinding, SourceBinding,
    ResolvedEmbeddingScope, EmbeddingContractError, parse_embedding_command,
    bind_embedding_targets,
)
from documents.processing.parse_artifacts import (
    current_parse_policy_fingerprint, parse_content_fingerprint,
)
from documents.processing.plan_adapter import (
    _current_membership, build_document_initial_plan, source_reference,
)
from documents.services.result_access import get_result_source, ResultAccessError
from documents.services.result_envelope import DOCUMENT_EXTRACT_RESULT_SCHEMA
from files.services.access import can_modify_node
from orchestration.artifact_bindings import ArtifactBinding, ResolvedArtifact, resolve_artifact_binding
from orchestration.preparation import InputReadiness, prepare_request


def _source_context(*, actor, workspace, membership, source_uid):
    membership = _current_membership(workspace, membership, actor=actor)
    node = get_result_source(workspace=workspace, membership=membership, source_uid=source_uid)
    if not can_modify_node(actor=actor, membership=membership, node=node) or not node.ai_processing_enabled:
        raise ResultAccessError("PROCESSING_ACCESS_DENIED", "Processing is not permitted", 403)
    source = source_reference(node)
    if not source.checksum:
        raise EmbeddingContractError("SOURCE_EVIDENCE_MISSING", "Source checksum is required")
    return node, membership, source


def _resolved_parse(node, source, *, check_policy=True):
    result = DocumentParseResult.objects.filter(node=node).first()
    if result is None:
        raise EmbeddingContractError("PARSE_MISSING", "Extraction result does not exist")
    metadata = result.metadata or {}
    if (result.status != AIStatus.COMPLETED or result.parse_revision is None
            or metadata.get("result_schema") != DOCUMENT_EXTRACT_RESULT_SCHEMA
            or metadata.get("source_blob_uuid") != source.revision
            or metadata.get("source_checksum") != source.checksum
            or not metadata.get("processing_fingerprint")):
        raise EmbeddingContractError("PARSE_NOT_READY", "Extraction lacks current explicit provenance")
    if check_policy and metadata.get("parse_policy_fingerprint") != current_parse_policy_fingerprint():
        raise EmbeddingContractError("PARSE_POLICY_CHANGED", "Extraction policy changed")
    chunks = list(result.chunks.order_by("chunk_index").values("id", "chunk_index", "text", "chunk_meta"))
    if (not chunks or len(chunks) != result.chunk_count
            or metadata.get("content_fingerprint") != parse_content_fingerprint(chunks)):
        raise EmbeddingContractError("PARSE_CONTENT_CHANGED", "Extraction chunks changed or are incomplete")
    return result, ResolvedArtifact(
        f"document-extract:{result.pk}", DOCUMENT_EXTRACT_RESULT_SCHEMA,
        str(result.parse_revision), source.source_id, source.revision, source.checksum,
        metadata["processing_fingerprint"], "ready", False,
        str((result.performance_metrics or {}).get("job_id") or "")), tuple(row["id"] for row in chunks)


def resolve_document_artifact(binding, *, actor, workspace, membership, source_uid,
                              producer_job_id=None):
    node, _, source = _source_context(actor=actor, workspace=workspace,
                                      membership=membership, source_uid=source_uid)
    _, artifact, _ = _resolved_parse(node, source)
    return resolve_artifact_binding(binding, source=source,
        resolve_authorized_artifact=lambda _: artifact, producer_job_id=producer_job_id)


def resolve_document_embedding_scope(command, *, actor, workspace, membership):
    from documents.services.embedding_runtime_config import load_embedding_runtime
    node, _, source = _source_context(actor=actor, workspace=workspace,
                                      membership=membership, source_uid=command.source.uid)
    _, artifact, chunk_ids = _resolved_parse(node, source)
    runtime = load_embedding_runtime()
    return ResolvedEmbeddingScope(
        SourceBinding(str(workspace.pk), source.source_id, source.revision, source.checksum),
        ParseBinding(artifact.artifact_id, artifact.schema, artifact.revision, artifact.processing_fingerprint),
        RuntimeBinding(runtime.generation_id, runtime.runtime_fingerprint), chunk_ids, True, False)


def resolve_document_execution_scope(command, *, actor, workspace, membership):
    """Read pinned input for execution; never reconsider current parser policy.

    Current access/AI permission is a revocation guard. Source/content/runtime
    changes still invalidate execution; deciding to reparse belongs to planning.
    """
    from documents.services.embedding_runtime_config import load_embedding_runtime
    node, _, source = _source_context(actor=actor, workspace=workspace,
        membership=membership, source_uid=command.source.uid)
    _, artifact, chunk_ids = _resolved_parse(node, source, check_policy=False)
    runtime = load_embedding_runtime()
    return ResolvedEmbeddingScope(
        SourceBinding(str(workspace.pk), source.source_id, source.revision, source.checksum),
        ParseBinding(artifact.artifact_id, artifact.schema, artifact.revision, artifact.processing_fingerprint),
        RuntimeBinding(runtime.generation_id, runtime.runtime_fingerprint), chunk_ids, True, False)


@dataclass(frozen=True)
class DocumentEmbeddingPreparation:
    action: str
    reason: str
    binding: ArtifactBinding
    command: EmbeddingCommand | None
    target_ids: tuple[int, ...]
    parse_plan: object | None = None


def prepare_document_embedding(*, actor, workspace, membership, source_uid, request_id,
                               selector=None, mode="missing", pinned_input=None, deadline_at=None):
    """Return reuse/parse decision; caller must journal before any publication.

    Pinned refresh never relocates to another parse. Missing prerequisites only
    produce a parse plan for an all-file preparation, never speculative chunk IDs.
    """
    # Common orchestration invokes validation before artifact inspection and
    # chooses the path. The domain adapter never publishes work.
    class Adapter:
        def validate_request(self):
            from documents.services.embedding_runtime_config import load_embedding_runtime
            if not isinstance(request_id, str) or not 0 < len(request_id) <= 64:
                raise ValueError("request_id must be a non-empty bounded string")
            node, current_membership, source = _source_context(actor=actor, workspace=workspace,
                membership=membership, source_uid=source_uid)
            runtime = load_embedding_runtime()
            selected = {"type": "all"} if selector is None else selector
            # This placeholder validates request shape and is never published.
            raw = {"schema": COMMAND_SCHEMA,
                "source": {"workspace_id": str(workspace.pk), "uid": source.source_id,
                           "revision": source.revision, "checksum": source.checksum},
                "input": {"artifact_id": "unresolved", "schema": DOCUMENT_EXTRACT_RESULT_SCHEMA,
                          "revision": "unresolved", "processing_fingerprint": "unresolved"},
                "selector": selected, "mode": mode,
                "runtime": {"generation_id": runtime.generation_id, "fingerprint": runtime.runtime_fingerprint}}
            parse_embedding_command(raw)
            return node, current_membership, source, raw

        def inspect_input(self, context):
            node, _, source, _ = context
            try:
                _, artifact, _ = _resolved_parse(node, source)
            except EmbeddingContractError as exc:
                if exc.code not in {"PARSE_MISSING", "PARSE_NOT_READY",
                                    "PARSE_POLICY_CHANGED", "PARSE_CONTENT_CHANGED"}:
                    raise
                return InputReadiness(False, exc.code, exc)
            return InputReadiness(True, "VALID_PARSE", artifact)

        def require_prerequisite_allowed(self, context, evidence):
            if pinned_input is not None or context[3]["selector"]["type"] != "all":
                raise evidence.value

        def compose_prerequisite(self, context, evidence):
            plan = build_document_initial_plan(actor=actor, workspace=workspace, membership=context[1],
                source_uid=source_uid, request_id=request_id, deadline_at=deadline_at)
            return DocumentEmbeddingPreparation("parse", evidence.reason,
                ArtifactBinding("step", "initial-extract", DOCUMENT_EXTRACT_RESULT_SCHEMA), None, (), plan)

        def compile_reuse(self, context, artifact):
            return _compile_reuse(artifact, raw=context[3], pinned_input=pinned_input,
                actor=actor, workspace=workspace, membership=context[1])

    return prepare_request(Adapter()).value


def _compile_reuse(artifact, *, raw, pinned_input, actor, workspace, membership):
    parsed_input = ParseBinding(artifact.artifact_id, artifact.schema, artifact.revision,
                               artifact.processing_fingerprint)
    if pinned_input is not None and pinned_input != parsed_input:
        raise EmbeddingContractError("PARSE_NOT_READY", "Pinned extraction changed")
    raw["input"] = {key: getattr(parsed_input, key) for key in ParseBinding.__dataclass_fields__}
    command = parse_embedding_command(raw)
    targets = bind_embedding_targets(command, resolve_authorized_scope=lambda value:
        resolve_document_embedding_scope(value, actor=actor, workspace=workspace, membership=membership))
    return DocumentEmbeddingPreparation("reuse", "VALID_PARSE",
        ArtifactBinding("artifact", artifact.artifact_id, artifact.schema, artifact.revision),
        command, targets)
