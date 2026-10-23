from dataclasses import replace
from unittest.mock import patch

import pytest

from capabilities.functions import get_function
from documents.models import DocumentEmbeddingManifest
from documents.processing.embedding_plan_adapter import build_document_embedding_plan
from orchestration.command_inputs import prepare_step_command
from orchestration.contracts import WorkContractError, validate_work_payload, validate_preparation_payload
from orchestration.submissions import submit_plan, publish_step
from tests.processing.test_embedding_preparation import deployment, doc, parsed

pytestmark = pytest.mark.django_db


@pytest.fixture
def admitted(monkeypatch):
    from capabilities.deployment import load_deployment_snapshot
    snapshot = load_deployment_snapshot()
    provider = {**get_function("text.embed").metadata(), "role": "embedding",
        "adapter_contract": "dotori-internal-embed-v1", "model_installation_id": "embedding:generation",
        "verification": "contract_verified"}
    # Declared synthetic provider, real snapshot selection and domain adapter.
    # No model/runtime inference or installation evidence is fabricated.
    snapshot = replace(snapshot,
        role_bindings={**snapshot.role_bindings, "embedding": {"enabled": True,
            "model_installation_id": "embedding:generation"}},
        function_providers={**snapshot.function_providers, "text.embed": [provider]},
        installed_models={**snapshot.installed_models, "embedding:generation": {"artifact_revision": "runtime"}},
        default_routes={**snapshot.default_routes, "text.embed": "embedding:generation"})
    monkeypatch.setattr("capabilities.deployment.load_deployment_snapshot", lambda: snapshot)
    monkeypatch.setattr("orchestration.plans.load_deployment_snapshot", lambda: snapshot)
    return snapshot


def intake(doc):
    plan = build_document_embedding_plan(actor=doc.actor, workspace=doc.workspace,
        membership=doc.membership, source_uid=doc.node.uid, request_id="journal-real")
    row = submit_plan(plan, actor=doc.actor, workspace=doc.workspace, preparation=True)
    return plan, row


def test_real_adapter_common_journal_and_manifest_commit_without_publication(doc, admitted):
    parsed(doc)
    with patch("orchestration.submissions._publish_after_commit") as publish:
        plan, row = intake(doc)
        command = prepare_step_command(row.pk, "embed")
    row.refresh_from_db()
    manifest = DocumentEmbeddingManifest.objects.get(plan=row)
    assert row.control["compiled_commands"]["embed"]["command"]["payload"] == command.payload
    assert command.payload["manifest"]["id"] == str(manifest.pk)
    assert prepare_step_command(row.pk, "embed") == command
    publish.assert_not_called()
    with pytest.raises(WorkContractError, match="cannot publish"):
        publish_step(row.pk, "embed")
    validate_work_payload("embed_document", command.payload)
    with pytest.raises(ValueError):
        submit_plan(plan, actor=doc.actor, workspace=doc.workspace)  # wrong publication mode


def test_common_journal_save_failure_rolls_back_domain_manifest(doc, admitted):
    from orchestration_state.models import PlanSubmission
    parsed(doc)
    plan, row = intake(doc)
    original = PlanSubmission.save
    def fail(instance, *args, **kwargs):
        if "compiled_commands" in instance.control:
            raise RuntimeError("journal write failed")
        return original(instance, *args, **kwargs)
    with patch.object(PlanSubmission, "save", fail):
        with pytest.raises(RuntimeError, match="journal write"):
            prepare_step_command(row.pk, "embed")
    assert not DocumentEmbeddingManifest.objects.exists()
    row.refresh_from_db()
    assert "compiled_commands" not in row.control


def test_missing_parse_journal_cannot_compile_before_ready_report(doc, admitted):
    plan, row = intake(doc)
    with pytest.raises(WorkContractError, match="dependencies"):
        prepare_step_command(row.pk, "embed")
    assert not DocumentEmbeddingManifest.objects.exists()
    with pytest.raises(WorkContractError, match="cannot publish"):
        publish_step(row.pk, "initial-extract")
    from dataclasses import asdict
    from orchestration.plans import command_for_step
    from orchestration.submissions import DELIVERY
    command = command_for_step(plan, "initial-extract")
    with pytest.raises(WorkContractError, match="cannot authorize"):
        DELIVERY.authorize(command.work_kind, asdict(command), command.payload)


def test_preparation_intent_is_not_delivery(doc, admitted):
    parsed(doc)
    plan, row = intake(doc)
    with pytest.raises(ValueError):
        validate_preparation_payload("embed_document", plan.steps[0].payload, phase="delivery")


def test_current_runtime_change_blocks_replay(doc, admitted):
    from types import SimpleNamespace
    parsed(doc)
    plan, row = intake(doc)
    command = prepare_step_command(row.pk, "embed")
    with patch("documents.services.embedding_runtime_config.load_embedding_runtime",
               return_value=SimpleNamespace(generation_id="other", runtime_fingerprint="other")):
        with pytest.raises(WorkContractError):
            prepare_step_command(row.pk, "embed")
    assert DocumentEmbeddingManifest.objects.get(plan=row).command == command.payload["command"]


def test_exact_parse_producer_report_allows_consumer_compilation(doc, admitted):
    from dataclasses import asdict
    from orchestration.plans import command_for_step
    from documents.services.plan_delivery import DOMAIN_ADAPTER
    plan, row = intake(doc)
    producer = command_for_step(plan, "initial-extract")
    result = parsed(doc)
    result.performance_metrics = {"job_id": producer.job_id}
    result.save(update_fields=["performance_metrics"])
    report = DOMAIN_ADAPTER.report(plan, row,
        {"job_id": producer.job_id, "work_kind": "parse_document", "status": "succeeded"})
    row.reports = {"initial-extract": asdict(report)}
    row.save(update_fields=["reports"])
    command = prepare_step_command(row.pk, "embed")
    assert command.payload["command"]["input"]["revision"] == str(result.parse_revision)
    assert result.performance_metrics["job_id"] == producer.job_id


def test_foreign_manifest_reference_rejected_on_replay(doc, admitted):
    parsed(doc)
    plan, row = intake(doc)
    prepare_step_command(row.pk, "embed")
    row.refresh_from_db()
    from uuid import uuid4
    row.control["compiled_commands"]["embed"]["command"]["payload"]["manifest"]["id"] = str(uuid4())
    row.save(update_fields=["control"])
    with pytest.raises(ValueError):
        prepare_step_command(row.pk, "embed")


def test_request_identity_rejects_changed_intent(doc, admitted):
    parsed(doc)
    intake(doc)
    changed = build_document_embedding_plan(actor=doc.actor, workspace=doc.workspace,
        membership=doc.membership, source_uid=doc.node.uid, request_id="journal-real", mode="refresh")
    with pytest.raises(ValueError, match="another plan"):
        submit_plan(changed, actor=doc.actor, workspace=doc.workspace, preparation=True)


def test_installed_revision_mismatch_rejects_intake(doc, admitted):
    parsed(doc)
    admitted.installed_models["embedding:generation"]["artifact_revision"] = "wrong"
    with pytest.raises(WorkContractError, match="server runtime"):
        intake(doc)
    from orchestration_state.models import PlanSubmission
    assert not PlanSubmission.objects.exists()
