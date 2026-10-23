"""Domain input pinning ports; no queue, model, attempts or route activation.

Common plan control must authorize admission/dependencies before approval and
authenticate delivery before execution validation. A manifest ID is not a token.
"""
from dataclasses import asdict
from contextlib import contextmanager
import hashlib
import json

from django.db import transaction

from documents.models import DocumentEmbeddingManifest, DocumentParseResult
from documents.processing.embedding_contract import (
    EmbeddingContractError, DOCUMENT_EMBED_WORK_KIND, DOCUMENT_EMBED_FUNCTION_ID,
    parse_embedding_command, parse_embedding_intent, bind_embedding_targets, validate_embedding_execution,
)
from documents.processing.embedding_preparation import (
    _source_context, resolve_document_artifact, resolve_document_embedding_scope,
    resolve_document_execution_scope,
)
from documents.processing.parse_artifacts import parse_content_fingerprint
from documents.services.plan_delivery import _context
from documents.services.result_access import ResultAccessError
from orchestration.plans import plan_from_record, command_for_step
from orchestration_state.models import PlanSubmission


def _reject(message):
    raise EmbeddingContractError("MANIFEST_MISMATCH", message)


@contextmanager
def _domain_errors():
    # ResultAccessError is a frozen dataclass exception. Translate inside the
    # transaction before generator context managers need to set its traceback.
    try:
        yield
    except ResultAccessError as exc:
        raise EmbeddingContractError(exc.code, str(exc)) from exc


def _fingerprint(command, targets, *, plan_id, step_id, job_id):
    value = {"schema": "dotori.document.embedding-manifest.v1", "plan_id": plan_id,
             "step_id": step_id, "job_id": job_id, "command": command, "targets": targets}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode()).hexdigest()


def _target_evidence(ids):
    from documents.models import DocumentChunk
    chunks = list(DocumentChunk.objects.filter(pk__in=ids).values(
        "id", "chunk_index", "text", "chunk_meta"))
    if {row["id"] for row in chunks} != set(ids):
        _reject("Approved chunks are unavailable")
    return [{"chunk_id": row["id"], "content_fingerprint": parse_content_fingerprint([row])}
            for row in sorted(chunks, key=lambda row: row["id"])]


def _check_record(manifest, command):
    if manifest.command != command.to_payload():
        _reject("Stored command differs from requested command")
    targets = manifest.targets
    if (not isinstance(targets, list) or not targets or manifest.target_count != len(targets)
            or any(not isinstance(row, dict) or set(row) != {"chunk_id", "content_fingerprint"}
                   or type(row["chunk_id"]) is not int or row["chunk_id"] < 1
                   or not isinstance(row["content_fingerprint"], str)
                   or len(row["content_fingerprint"]) != 64 for row in targets)):
        _reject("Malformed manifest targets")
    ids = tuple(row["chunk_id"] for row in targets)
    if ids != tuple(sorted(set(ids))):
        _reject("Manifest targets must be canonical and distinct")
    if manifest.fingerprint != _fingerprint(manifest.command, targets,
            plan_id=manifest.plan_id, step_id=manifest.step_id, job_id=manifest.job_id):
        _reject("Manifest fingerprint changed")
    return ids


def approve_embedding_manifest(*, plan_id, step_id, command):
    """Pin approved domain input inside compiler transaction; replay never selects.

    This port does not admit/publish a plan. Caller must use common plan control.
    The composition template is an intent without speculative input identity;
    source/selector/mode/runtime cannot change.
    """
    command = parse_embedding_command(command.to_payload())
    with transaction.atomic(), _domain_errors():
        row = PlanSubmission.objects.select_for_update().get(pk=plan_id)
        plan = plan_from_record(row.definition)
        if plan.plan_id != row.pk:
            _reject("Plan journal identity changed")
        step = next((step for step in plan.steps if step.step_id == step_id), None)
        if (step is None or step.work_kind != DOCUMENT_EMBED_WORK_KIND
                or step.function_id != DOCUMENT_EMBED_FUNCTION_ID or step.input_binding is None):
            _reject("Manifest requires a bound document embedding step")
        template = parse_embedding_intent(step.payload)
        if (template.source != command.source or template.selector != command.selector
                or template.mode != command.mode or template.runtime != command.runtime):
            _reject("Command intent is outside the approved step")
        context = _context(row)
        node, _, source = _source_context(source_uid=row.source_uid, **context)
        if source != plan.request.source or str(row.workspace_id) != command.source.workspace_id:
            _reject("Command source is outside the approved plan")
        from files.models import Node, FileBlob
        Node.objects.select_for_update().get(pk=node.pk)
        list(FileBlob.objects.select_for_update().filter(node_id=node.pk))
        list(DocumentParseResult.objects.select_for_update().filter(node_id=node.pk))
        producer = (command_for_step(plan, step.input_binding.reference).job_id
                    if step.input_binding.kind == "step" else None)
        artifact = resolve_document_artifact(step.input_binding, source_uid=row.source_uid,
                                             producer_job_id=producer, **context)
        if asdict(command.input) != {key: getattr(artifact, key) for key in
                ("artifact_id", "schema", "revision", "processing_fingerprint")}:
            _reject("Command input differs from the approved artifact")
        job = command_for_step(plan, step_id).job_id
        existing = DocumentEmbeddingManifest.objects.filter(plan=row, step_id=step_id).first()
        resolver = lambda value: resolve_document_embedding_scope(value, **context)
        if existing is not None:
            if existing.job_id != job or existing.node_id != node.pk:
                _reject("Stored manifest identity changed")
            ids = _check_record(existing, command)
            validate_embedding_execution(command, approved_target_ids=ids,
                                         resolve_execution_input=resolver)
            if _target_evidence(ids) != existing.targets:
                _reject("Stored target content changed")
            return existing
        ids = bind_embedding_targets(command, resolve_authorized_scope=resolver)
        targets = _target_evidence(ids)
        payload = command.to_payload()
        return DocumentEmbeddingManifest.objects.create(plan=row, node=node, step_id=step_id,
            job_id=job, command=payload, targets=targets, target_count=len(ids),
            fingerprint=_fingerprint(payload, targets, plan_id=row.pk, step_id=step_id, job_id=job))


def validate_execution_manifest(*, manifest_id, fingerprint, plan_id, step_id, job_id, command):
    """Validate fixed input, not parser policy or request appropriateness.

    Returns the complete approved scope. Batch subset/attempt/final-write guards
    belong to the future handler; this is not a delivery authorization endpoint.
    """
    command = parse_embedding_command(command.to_payload())
    manifest = DocumentEmbeddingManifest.objects.select_related(
        "plan__created_by", "plan__workspace").get(pk=manifest_id)
    if (manifest.plan_id != plan_id or manifest.step_id != step_id or manifest.job_id != job_id
            or manifest.fingerprint != fingerprint):
        _reject("Manifest does not belong to the authenticated command")
    ids = _check_record(manifest, command)
    context = _context(manifest.plan)
    with _domain_errors():
        node, _, _ = _source_context(source_uid=command.source.uid, **context)
        if node.pk != manifest.node_id:
            _reject("Manifest source changed")
        validate_embedding_execution(command, approved_target_ids=ids,
            resolve_execution_input=lambda value: resolve_document_execution_scope(value, **context))
    if _target_evidence(ids) != manifest.targets:
        _reject("Approved content changed")
    return ids
