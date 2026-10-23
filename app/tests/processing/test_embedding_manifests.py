from dataclasses import replace
from unittest.mock import patch

import pytest
from django.db import transaction

from documents.models import DocumentEmbeddingManifest
from documents.processing.embedding_contract import EmbeddingContractError, EmbeddingIntent
from documents.processing.embedding_manifests import (
    approve_embedding_manifest, validate_execution_manifest,
)
from orchestration.plans import PlanStep, plan_from_record, plan_record
from tests.processing.test_embedding_preparation import deployment, doc, parsed, prepare, submission

pytestmark = pytest.mark.django_db


def intent(command):
    return EmbeddingIntent(command.source, command.selector, command.mode, command.runtime).to_payload()


@pytest.fixture
def approval(doc):
    parsed(doc)
    prepared = prepare(doc)
    row = submission(doc)
    old = plan_from_record(row.definition)
    step = PlanStep("embed", "embed_document", "document.embed", prepared.binding.schema,
        "dotori.document.embedding-result.v1", intent(prepared.command),
        input_binding=prepared.binding)
    # Inactive domain port fixture, not registered-work admission or publication.
    plan = replace(old, version=2, steps=(step,))
    row.definition = plan_record(plan)
    row.save(update_fields=["definition"])
    return row, prepared.command


def approve(approval):
    row, command = approval
    return approve_embedding_manifest(plan_id=row.pk, step_id="embed", command=command)


def execute(manifest, command, **overrides):
    args = dict(manifest_id=manifest.pk, fingerprint=manifest.fingerprint,
        plan_id=manifest.plan_id, step_id=manifest.step_id, job_id=manifest.job_id, command=command)
    return validate_execution_manifest(**{**args, **overrides})


def test_manifest_durable_canonical_and_replay_does_not_select(approval):
    manifest = approve(approval)
    assert manifest.target_count == 2
    assert len(manifest.fingerprint) == 64
    with patch("documents.processing.embedding_manifests.bind_embedding_targets",
               side_effect=AssertionError("Replay must not select targets")):
        again = approve(approval)
    assert again.pk == manifest.pk
    assert DocumentEmbeddingManifest.objects.count() == 1
    assert execute(manifest, approval[1]) == tuple(row["chunk_id"] for row in manifest.targets)


def test_manifest_rolls_back_with_compiler_transaction(approval):
    with pytest.raises(RuntimeError), transaction.atomic():
        approve(approval)
        raise RuntimeError("journal save failed")
    assert not DocumentEmbeddingManifest.objects.exists()


def test_execution_never_reconsiders_parser_policy(approval):
    manifest = approve(approval)
    with patch("documents.processing.embedding_preparation.current_parse_policy_fingerprint",
               side_effect=AssertionError("No replan")):
        assert len(execute(manifest, approval[1])) == 2
        with pytest.raises(AssertionError):
            approve(approval)  # Publication validation still owns current policy.


@pytest.mark.parametrize("field,value", [("job_id", "wrong-job"), ("step_id", "wrong-step"),
                                        ("plan_id", "wrong-plan"), ("fingerprint", "wrong")])
def test_wrong_delivery_identity_rejected(approval, field, value):
    manifest = approve(approval)
    with pytest.raises(EmbeddingContractError):
        execute(manifest, approval[1], **{field: value})


@pytest.mark.parametrize("mutation", ["targets", "count", "command", "fingerprint"])
def test_manifest_corruption_rejected(approval, mutation):
    manifest = approve(approval)
    if mutation == "targets":
        manifest.targets = manifest.targets[:1]
        manifest.save(update_fields=["targets"])
    elif mutation == "count":
        manifest.target_count += 1
        manifest.save(update_fields=["target_count"])
    elif mutation == "command":
        manifest.command["mode"] = "refresh"
        manifest.save(update_fields=["command"])
    else:
        manifest.fingerprint = "x" * 64
        manifest.save(update_fields=["fingerprint"])
    with pytest.raises(EmbeddingContractError):
        execute(manifest, approval[1])
    with pytest.raises(EmbeddingContractError):
        approve(approval)


def test_reparse_keeps_old_manifest_but_never_rebinds(approval, doc):
    manifest = approve(approval)
    old_targets = manifest.targets
    parsed(doc)
    manifest.refresh_from_db()
    assert manifest.targets == old_targets
    with pytest.raises(EmbeddingContractError):
        execute(manifest, approval[1])
    with pytest.raises(ValueError):
        approve(approval)
    assert DocumentEmbeddingManifest.objects.count() == 1


def test_changed_intent_cannot_reuse_same_step(approval):
    row, command = approval
    approve(approval)
    with pytest.raises(EmbeddingContractError):
        approve_embedding_manifest(plan_id=row.pk, step_id="embed",
                                   command=replace(command, mode="refresh"))


def test_permission_revocation_blocks_both_ports(approval, doc):
    manifest = approve(approval)
    type(doc.membership).objects.filter(pk=doc.membership.pk).update(status="revoked")
    with pytest.raises(EmbeddingContractError):
        approve(approval)
    with pytest.raises(EmbeddingContractError):
        execute(manifest, approval[1])


def test_chunk_mutation_blocks_execution(approval, doc):
    manifest = approve(approval)
    doc.node.parse_result.chunks.filter(pk=manifest.targets[0]["chunk_id"]).update(text="changed")
    with pytest.raises(EmbeddingContractError):
        execute(manifest, approval[1])


def test_runtime_change_blocks_execution_without_rebinding(approval):
    from types import SimpleNamespace
    manifest = approve(approval)
    with patch("documents.services.embedding_runtime_config.load_embedding_runtime",
               return_value=SimpleNamespace(generation_id="new-generation", runtime_fingerprint="new-runtime")):
        with pytest.raises(EmbeddingContractError):
            execute(manifest, approval[1])
        with pytest.raises(EmbeddingContractError):
            approve(approval)
    manifest.refresh_from_db()
    assert manifest.command == approval[1].to_payload()


def test_explicit_selection_freezes_only_requested_chunk(approval):
    from documents.processing.embedding_contract import ChunkSelector
    row, command = approval
    plan = plan_from_record(row.definition)
    from documents.models import DocumentChunk
    selected = DocumentChunk.objects.order_by("id").first().pk
    command = replace(command, selector=ChunkSelector("chunk_ids", (selected,)))
    plan = replace(plan, steps=(replace(plan.steps[0], payload=intent(command)),))
    row.definition = plan_record(plan)
    row.save(update_fields=["definition"])
    manifest = approve((row, command))
    assert manifest.target_count == 1
    assert execute(manifest, command) == (selected,)


def test_delete_plan_cascades_manifest(approval):
    approve(approval)
    approval[0].delete()
    assert not DocumentEmbeddingManifest.objects.exists()
