from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.db import transaction

from capabilities.deployment import clear_deployment_snapshot_cache
from config.enums import AIStatus
from documents.models import DocumentChunk, DocumentParseResult
from documents.parsers.schema import ChunkPayload, ParseResult
from documents.processing.embedding_contract import EmbeddingContractError, ParseBinding
from documents.processing.embedding_preparation import (
    prepare_document_embedding, resolve_document_artifact,
    resolve_document_execution_scope,
)
from documents.processing.parsing import save_parse_result
from documents.services.result_access import ResultAccessError
from installation.deployment import build_module_deployment_plan, write_deployment_plan
from orchestration.artifact_bindings import ArtifactBinding

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def deployment(tmp_path, monkeypatch):
    path = write_deployment_plan(build_module_deployment_plan(["documents"]), repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    clear_deployment_snapshot_cache()
    with patch("documents.processing.parse_artifacts.current_parse_policy_fingerprint", return_value="policy"), \
         patch("documents.processing.embedding_preparation.current_parse_policy_fingerprint", return_value="policy"), \
         patch("documents.processing.parsing._parse_metadata", return_value={"parser_backend": "fake", "parser_version": "1"}), \
         patch("documents.services.embedding_runtime_config.load_embedding_runtime", return_value=SimpleNamespace(
             generation_id="generation", runtime_fingerprint="runtime")):
        yield
    clear_deployment_snapshot_cache()


@pytest.fixture
def doc():
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from files.models import Node, FileBlob
    from workspaces.models import Workspace, WorkspaceMembership
    actor = get_user_model().objects.create_user(email="preparation@example.com", password="test")
    workspace = Workspace.objects.create(name="preparation", created_by=actor)
    membership = WorkspaceMembership.objects.create(user=actor, workspace=workspace, role="admin")
    node = Node.objects.create(owner=actor, workspace=workspace, name="source.txt", ext=".txt", node_type="file")
    with patch("documents.signals._publish_uploaded_blob"):
        blob = FileBlob.objects.create(node=node, original_name="source.txt", mime_type="text/plain",
            size=4, sha256="checksum", status="ready", file=SimpleUploadedFile("source.txt", b"text"))
    return SimpleNamespace(actor=actor, workspace=workspace, membership=membership, node=node, blob=blob)


def parsed(doc, status="success"):
    result = save_parse_result(doc.node, ParseResult(parser_mode="convert", parser_backend="fake",
        parser_version="1", file_path="source.txt", file_ext=".txt", status=status,
        chunks=[ChunkPayload(chunk_index=0, serialized_text="first"),
                ChunkPayload(chunk_index=1, serialized_text="second")]))
    result.performance_metrics = {"job_id": "creator-job"}
    result.save(update_fields=["performance_metrics"])
    return result


def prepare(doc, **kwargs):
    return prepare_document_embedding(actor=doc.actor, workspace=doc.workspace,
        membership=doc.membership, source_uid=doc.node.uid, request_id="request", **kwargs)


def test_ready_parse_reuses_without_publish_and_preserves_creator(doc):
    result = parsed(doc)
    with patch("orchestration.dispatch.publish_registered_work") as publish:
        preparation = prepare(doc)
    assert preparation.action == "reuse"
    assert preparation.parse_plan is None
    assert len(preparation.target_ids) == 2
    assert preparation.command.input.revision == str(result.parse_revision)
    publish.assert_not_called()
    result.refresh_from_db()
    assert result.performance_metrics["job_id"] == "creator-job"
    artifact = resolve_document_artifact(preparation.binding, actor=doc.actor,
        workspace=doc.workspace, membership=doc.membership, source_uid=doc.node.uid)
    assert artifact.producer_job_id == "creator-job"


def test_execution_checks_fixed_input_without_replanning_parser_policy(doc):
    from documents.processing.embedding_contract import validate_embedding_execution
    parsed(doc)
    prepared = prepare(doc)
    with patch("documents.processing.embedding_preparation.current_parse_policy_fingerprint",
               side_effect=AssertionError("Executor must not reconsider parser policy")), \
         patch("documents.processing.embedding_preparation.build_document_initial_plan") as plan:
        assert validate_embedding_execution(prepared.command, approved_target_ids=prepared.target_ids,
            resolve_execution_input=lambda command: resolve_document_execution_scope(command,
                actor=doc.actor, workspace=doc.workspace, membership=doc.membership)) == prepared.target_ids
    plan.assert_not_called()


def test_execution_source_change_rejected_without_parse_fallback(doc):
    from documents.processing.embedding_contract import validate_embedding_execution
    parsed(doc)
    prepared = prepare(doc)
    type(doc.blob).objects.filter(pk=doc.blob.pk).update(sha256="changed")
    with patch("documents.processing.embedding_preparation.build_document_initial_plan") as plan:
        with pytest.raises(EmbeddingContractError):
            validate_embedding_execution(prepared.command, approved_target_ids=prepared.target_ids,
                resolve_execution_input=lambda command: resolve_document_execution_scope(command,
                    actor=doc.actor, workspace=doc.workspace, membership=doc.membership))
    plan.assert_not_called()


def test_unexpected_artifact_error_never_schedules_parse(doc):
    with patch("documents.processing.embedding_preparation._resolved_parse",
               side_effect=EmbeddingContractError("STORAGE_UNAVAILABLE", "storage failed")), \
         patch("documents.processing.embedding_preparation.build_document_initial_plan") as plan:
        with pytest.raises(EmbeddingContractError) as error:
            prepare(doc)
    assert error.value.code == "STORAGE_UNAVAILABLE"
    plan.assert_not_called()


def test_execution_permission_revocation_still_blocks_fixed_command(doc):
    parsed(doc)
    prepared = prepare(doc)
    type(doc.membership).objects.filter(pk=doc.membership.pk).update(status="revoked")
    with pytest.raises(ResultAccessError):
        resolve_document_execution_scope(prepared.command, actor=doc.actor,
            workspace=doc.workspace, membership=doc.membership)


def test_missing_and_legacy_parse_prepare_parse_plan_without_fake_command(doc):
    first = prepare(doc)
    assert first.action == "parse" and first.reason == "PARSE_MISSING"
    assert first.command is None and first.target_ids == ()
    assert first.binding.kind == "step"
    DocumentParseResult.objects.create(node=doc.node, status=AIStatus.COMPLETED, chunk_count=0)
    assert prepare(doc).reason == "PARSE_NOT_READY"


def test_reparse_changes_revision_even_failed_replacement_and_rolls_back_atomically(doc):
    old = parsed(doc)
    old_revision = old.parse_revision
    old_ids = list(old.chunks.values_list("id", flat=True))
    with pytest.raises(RuntimeError), transaction.atomic():
        parsed(doc)
        raise RuntimeError("rollback")
    old.refresh_from_db()
    assert old.parse_revision == old_revision
    assert list(old.chunks.values_list("id", flat=True)) == old_ids
    failed = parsed(doc, status="failed")
    assert failed.pk == old.pk and failed.parse_revision != old_revision
    assert failed.status == AIStatus.FAILED


def test_pinned_refresh_never_relocates_after_reparse(doc):
    parsed(doc)
    preparation = prepare(doc)
    parsed(doc)
    with pytest.raises(EmbeddingContractError):
        prepare(doc, mode="refresh", pinned_input=preparation.command.input)


@pytest.mark.parametrize("mutation,reason", [
    ("source", "PARSE_NOT_READY"), ("text", "PARSE_CONTENT_CHANGED"),
    ("delete", "PARSE_CONTENT_CHANGED"), ("policy", "PARSE_POLICY_CHANGED"),
])
def test_changed_input_reparse_for_file_but_rejects_pinned_scope(doc, mutation, reason):
    result = parsed(doc)
    pinned = prepare(doc).command.input
    if mutation == "source":
        type(doc.blob).objects.filter(pk=doc.blob.pk).update(sha256="changed")
    elif mutation == "text":
        result.chunks.filter(chunk_index=0).update(text="changed")
    elif mutation == "delete":
        result.chunks.filter(chunk_index=0).delete()
    else:
        result.metadata["parse_policy_fingerprint"] = "changed"
        result.save(update_fields=["metadata"])
    assert prepare(doc).reason == reason
    with pytest.raises(EmbeddingContractError):
        prepare(doc, pinned_input=pinned, mode="refresh")


def test_scope_rejects_other_chunk_and_invalid_selection_before_parsing(doc):
    with pytest.raises(EmbeddingContractError):
        prepare(doc, selector={"type": "chunk_ids", "ids": []})
    parsed(doc)
    with pytest.raises(EmbeddingContractError) as error:
        prepare(doc, selector={"type": "chunk_ids", "ids": [999999]})
    assert error.value.code == "CHUNK_OUTSIDE_ARTIFACT"


def test_membership_requeried_and_actor_cannot_borrow_admin_membership(doc):
    from django.contrib.auth import get_user_model
    parsed(doc)
    other = get_user_model().objects.create_user(email="other@example.com", password="test")
    with pytest.raises(ResultAccessError):
        prepare_document_embedding(actor=other, workspace=doc.workspace, membership=doc.membership,
            source_uid=doc.node.uid, request_id="request")
    type(doc.membership).objects.filter(pk=doc.membership.pk).update(status="revoked")
    with pytest.raises(ResultAccessError):
        prepare(doc)


def test_cross_workspace_and_wrong_producer_artifact_rejected(doc):
    from workspaces.models import Workspace
    result = parsed(doc)
    with pytest.raises(ValueError):
        resolve_document_artifact(ArtifactBinding("step", "extract", "dotori.document.extract.v1"),
            actor=doc.actor, workspace=doc.workspace, membership=doc.membership,
            source_uid=doc.node.uid, producer_job_id="other-job")
    workspace = Workspace.objects.create(name="other", created_by=doc.actor)
    with pytest.raises(ResultAccessError):
        prepare_document_embedding(actor=doc.actor, workspace=workspace, membership=doc.membership,
            source_uid=doc.node.uid, request_id="request")


def submission(doc):
    from documents.services.plan_delivery import submit_document_plan
    with patch("orchestration.submissions._publish_after_commit"):
        return submit_document_plan(actor=doc.actor, workspace=doc.workspace,
            membership=doc.membership, source_uid=doc.node.uid, request_id="journal")


def test_common_input_journal_freezes_reused_provenance_and_revalidates(doc):
    from orchestration.artifact_inputs import pin_submission_artifact
    result = parsed(doc)
    prepared = prepare(doc)
    row = submission(doc)
    first = pin_submission_artifact(row.pk, prepared.binding)
    assert first["artifact"]["producer_job_id"] == "creator-job"
    assert pin_submission_artifact(row.pk, prepared.binding) == first
    row.refresh_from_db()
    assert len(row.control["artifact_inputs"]) == 1
    parsed(doc)
    with pytest.raises(ValueError):
        pin_submission_artifact(row.pk, prepared.binding)
    row.refresh_from_db()
    assert next(iter(row.control["artifact_inputs"].values())) == first


def test_step_input_requires_report_and_keeps_input_journal_on_report_replay(doc):
    from documents.services.plan_delivery import ADAPTER
    from orchestration.artifact_inputs import pin_submission_artifact
    from orchestration.contracts import WorkContractError
    from orchestration.plans import plan_from_record, command_for_step
    result = parsed(doc)
    row = submission(doc)
    binding = ArtifactBinding("step", "initial-extract", "dotori.document.extract.v1")
    with pytest.raises(WorkContractError):
        pin_submission_artifact(row.pk, binding)
    command = command_for_step(plan_from_record(row.definition), "initial-extract")
    result.performance_metrics = {"job_id": command.job_id}
    result.save(update_fields=["performance_metrics"])
    envelope = {"plan_id": row.plan_id, "step_id": command.step_id,
                "job_id": command.job_id, "deadline_at": command.deadline_at,
                "work_kind": command.work_kind, "payload": command.payload}
    response = {"job_id": command.job_id, "work_kind": command.work_kind, "status": "succeeded"}
    ADAPTER.record_response(command.work_kind, envelope, command.payload, response)
    pinned = pin_submission_artifact(row.pk, binding)
    assert pinned["artifact"]["producer_job_id"] == command.job_id
    ADAPTER.record_response(command.work_kind, envelope, command.payload, response)
    row.refresh_from_db()
    assert next(iter(row.control["artifact_inputs"].values())) == pinned


def test_input_journal_rolls_back_and_rechecks_revoked_actor(doc):
    from orchestration.artifact_inputs import pin_submission_artifact
    from orchestration.contracts import WorkContractError
    parsed(doc)
    prepared = prepare(doc)
    row = submission(doc)
    with pytest.raises(RuntimeError), transaction.atomic():
        pin_submission_artifact(row.pk, prepared.binding)
        raise RuntimeError("rollback")
    row.refresh_from_db()
    assert not row.control.get("artifact_inputs")
    type(doc.membership).objects.filter(pk=doc.membership.pk).update(status="revoked")
    with pytest.raises(WorkContractError):
        pin_submission_artifact(row.pk, prepared.binding)
