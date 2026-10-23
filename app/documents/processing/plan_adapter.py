"""Internal static extraction template and existing authorized result adapter.

This supplies document source/command checks to common initial-plan composition,
not a new public parse-only feature. Multi-step control remains P2-C.
"""
from uuid import uuid4

from files.services.access import can_modify_node
from documents.services.result_access import get_result_source, list_source_results, get_result, ResultAccessError
from orchestration.plans import (
    SourceReference, validate_plan,
    command_for_step, report_from_receipt, PlanValidationError,
)


def source_reference(node):
    if not hasattr(node, "blob") or not node.blob.file:
        raise ResultAccessError("RESULT_SOURCE_NOT_FOUND", "Source has no file blob", 404)
    return SourceReference(str(node.uid), str(node.blob.uuid),
                           "dotori.document.source.v1", str(node.blob.sha256 or ""))


def build_document_initial_plan(*, actor, workspace, membership, source_uid, request_id=None, deadline_at=None):
    node = get_result_source(workspace=workspace, membership=membership, source_uid=source_uid)
    source = source_reference(node)
    from orchestration.templates import initial_plan
    plan = initial_plan(source=source, request_id=request_id or str(uuid4()),
        work_kind="parse_document", function_id="document.extract",
        payload={"node_id": node.id}, deadline_at=deadline_at, step_id="initial-extract")
    return validate_document_plan(plan, actor=actor, workspace=workspace, membership=membership)


def validate_document_plan(plan, *, actor, workspace, membership, work_kind=None, preparation=False):
    membership = _current_membership(workspace, membership, actor=actor)
    def authorize(source):
        node = get_result_source(workspace=workspace, membership=membership, source_uid=source.source_id)
        if not can_modify_node(actor=actor, membership=membership, node=node) or not node.ai_processing_enabled:
            raise ResultAccessError("PROCESSING_ACCESS_DENIED", "Processing is not permitted", 403)
        return source_reference(node)

    def validate_step(step, source):
        if work_kind is not None and step.work_kind != work_kind:
            return  # Its declared adapter owns its domain checks.
        node = get_result_source(workspace=workspace, membership=membership, source_uid=source.source_id)
        if (step.work_kind != "parse_document" or step.function_id != "document.extract"
                or step.payload != {"node_id": node.id}):
            raise ValueError("Document template payload must address the authorized source")

    def resolve_input(step, binding):
        from documents.processing.embedding_preparation import resolve_document_artifact
        return resolve_document_artifact(binding, actor=actor, workspace=workspace,
            membership=membership, source_uid=plan.request.source.source_id)

    return validate_plan(plan, authorize_source=authorize, validate_domain_step=validate_step,
        resolve_input=resolve_input, preparation=preparation)


def document_step_report(plan, receipt, *, workspace, membership):
    membership = _current_membership(workspace, membership)
    step_id = next((step.step_id for step in plan.steps
        if step.work_kind == receipt.get("work_kind")
        and command_for_step(plan, step.step_id).job_id == receipt.get("job_id")), None)
    if step_id is None:
        raise PlanValidationError("Receipt identity does not match a document step")
    artifacts = []
    if receipt.get("status") == "succeeded":
        candidates = list_source_results(workspace=workspace, membership=membership,
                                         source_uid=plan.request.source.source_id)
        # Link only a domain result persisted by this exact job, never an older
        # successful result for the same source.
        from documents.models import DocumentParseResult
        command = command_for_step(plan, step_id)
        for candidate in candidates:
            result_id = int(candidate["id"].split(":", 1)[1])
            if DocumentParseResult.objects.filter(pk=result_id,
                    performance_metrics__job_id=command.job_id).exists():
                artifacts.append(get_result(workspace=workspace, membership=membership, result_id=candidate["id"]))
    else:
        get_result_source(workspace=workspace, membership=membership, source_uid=plan.request.source.source_id)
    return report_from_receipt(plan, step_id, receipt, artifacts)


def compile_document_command(plan, *, actor, workspace, membership):
    """Revalidate before compiling; does not publish or authorize followups."""
    validate_document_plan(plan, actor=actor, workspace=workspace, membership=membership)
    return command_for_step(plan, "initial-extract")


def _current_membership(workspace, membership, actor=None):
    from workspaces.models import WorkspaceMembership
    if membership is None or (actor is not None and membership.user_id != actor.id):
        raise ResultAccessError("WORKSPACE_ACCESS_DENIED", "Membership does not belong to actor", 403)
    current = WorkspaceMembership.objects.filter(pk=membership.pk, workspace_id=workspace.id,
        user_id=membership.user_id, status=WorkspaceMembership.STATUS_ACTIVE).first()
    if current is None:
        raise ResultAccessError("WORKSPACE_ACCESS_DENIED", "Membership is no longer active", 403)
    return current
