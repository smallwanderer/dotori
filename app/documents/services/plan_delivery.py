"""Documents checks for the common plan submission service."""
from contextlib import contextmanager

from documents.processing.plan_adapter import (
    build_document_initial_plan, validate_document_plan, document_step_report,
)
from documents.services.result_access import ResultAccessError
from orchestration.contracts import WorkContractError
from orchestration.submissions import publish_submission, DELIVERY as ADAPTER


def _context(row):
    from workspaces.models import WorkspaceMembership
    membership = WorkspaceMembership.objects.filter(workspace_id=row.workspace_id,
        user_id=row.created_by_id, status=WorkspaceMembership.STATUS_ACTIVE).first()
    return dict(actor=row.created_by, workspace=row.workspace, membership=membership)


def submit_document_plan(*, actor, workspace, membership, source_uid, request_id, deadline_at=None):
    from orchestration.submissions import submit_plan
    if not isinstance(request_id, str) or not 0 < len(request_id) <= 64:
        raise ValueError("request_id must be a non-empty string up to 64 characters")
    plan = build_document_initial_plan(actor=actor, workspace=workspace, membership=membership,
        source_uid=source_uid, request_id=request_id, deadline_at=deadline_at)
    return submit_plan(plan, actor=actor, workspace=workspace, deadline_at=deadline_at)


class DocumentPlanAdapter:
    def validate_preparation(self, plan, row):
        try:
            return validate_document_plan(plan, work_kind="parse_document", preparation=True, **_context(row))
        except (ValueError, ResultAccessError) as exc:
            raise WorkContractError(str(exc)) from exc

    def resolve_input(self, plan, row, binding, *, producer_job_id=None):
        from documents.processing.embedding_preparation import resolve_document_artifact
        return resolve_document_artifact(binding, source_uid=plan.request.source.source_id,
            producer_job_id=producer_job_id, **_context(row))

    def validate(self, plan, row):
        try:
            return validate_document_plan(plan, work_kind="parse_document", **_context(row))
        except (ValueError, ResultAccessError) as exc:
            raise WorkContractError(str(exc)) from exc

    @contextmanager
    def source_guard(self, plan, row):
        from files.models import Node, FileBlob
        try:
            node = Node.objects.select_for_update().get(uid=row.source_uid, workspace_id=row.workspace_id)
        except Node.DoesNotExist as exc:
            raise WorkContractError("Plan source no longer exists") from exc
        list(FileBlob.objects.select_for_update().filter(node_id=node.pk))
        yield

    def report(self, plan, row, response):
        context = _context(row)
        try:
            return document_step_report(plan, response,
                workspace=context["workspace"], membership=context["membership"])
        except (ValueError, ResultAccessError) as exc:
            raise WorkContractError(str(exc)) from exc


DOMAIN_ADAPTER = DocumentPlanAdapter()
