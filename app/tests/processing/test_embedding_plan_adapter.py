from unittest.mock import patch
from dataclasses import replace

import pytest
from django.db import transaction

from documents.models import DocumentEmbeddingManifest
from documents.processing.embedding_plan_adapter import (
    build_document_embedding_plan, DocumentEmbeddingCompiler,
)
from orchestration.plans import plan_record
from tests.processing.test_embedding_preparation import deployment, doc, parsed, submission

pytestmark = pytest.mark.django_db


def build(doc):
    return build_document_embedding_plan(actor=doc.actor, workspace=doc.workspace,
        membership=doc.membership, source_uid=doc.node.uid, request_id="journal")


def test_real_document_reuse_template_compiler_and_manifest(doc):
    parsed(doc)
    plan = build(doc)
    assert len(plan.steps) == 1 and plan.steps[0].input_binding.kind == "artifact"
    assert "input" not in plan.steps[0].payload
    row = submission(doc)
    # Non-public harness: no registered-work admission/journal integration claim.
    plan = replace(plan, plan_id=row.pk)
    row.definition = plan_record(plan)
    row.save(update_fields=["definition"])
    compiler = DocumentEmbeddingCompiler()
    step = plan.steps[0]
    artifact = compiler.resolve_input(plan, row, step.input_binding)
    with patch("orchestration.dispatch.publish_registered_work") as publish:
        with transaction.atomic():
            payload = compiler.compile_command(plan, row, step, artifact)
            compiler.validate_compiled_command(plan, row, step, payload, artifact)
    assert len(payload["manifest"]["fingerprint"]) == 64
    assert compiler.compile_command(plan, row, step, artifact) == payload
    assert DocumentEmbeddingManifest.objects.count() == 1
    publish.assert_not_called()


def test_missing_parse_plan_has_real_dependency_without_fake_input(doc):
    plan = build(doc)
    assert [step.step_id for step in plan.steps] == ["initial-extract", "embed"]
    assert plan.steps[1].depends_on == ("initial-extract",)
    assert plan.steps[1].input_binding.kind == "step"
    assert "input" not in plan.steps[1].payload


def test_facade_delegates_to_common_without_legacy_plan_composition(doc):
    with patch("documents.processing.embedding_preparation.build_document_initial_plan",
               side_effect=AssertionError("Legacy composition must not be invoked")):
        plan = build(doc)
    assert len(plan.steps) == 2


def test_explicit_scope_missing_input_never_composes(doc):
    from documents.processing.embedding_contract import EmbeddingContractError
    with patch("orchestration.templates.bound_composition_plan") as compose:
        with pytest.raises(EmbeddingContractError):
            build_document_embedding_plan(actor=doc.actor, workspace=doc.workspace,
                membership=doc.membership, source_uid=doc.node.uid, request_id="scope",
                selector={"type": "chunk_ids", "ids": [99]})
    compose.assert_not_called()


def test_compiler_manifest_rolls_back_with_outer_transaction(doc):
    parsed(doc)
    plan = build(doc)
    row = submission(doc)
    plan = replace(plan, plan_id=row.pk)
    row.definition = plan_record(plan)
    row.save(update_fields=["definition"])
    compiler = DocumentEmbeddingCompiler()
    step = plan.steps[0]
    artifact = compiler.resolve_input(plan, row, step.input_binding)
    with pytest.raises(RuntimeError), transaction.atomic():
        compiler.compile_command(plan, row, step, artifact)
        raise RuntimeError("common journal failure")
    assert not DocumentEmbeddingManifest.objects.exists()
