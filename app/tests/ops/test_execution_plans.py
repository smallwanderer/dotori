from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from capabilities.deployment import clear_deployment_snapshot_cache
from installation.deployment import build_module_deployment_plan, write_deployment_plan
from orchestration.plans import (
    SourceReference, ProcessingRequest, PlanStep, static_plan, validate_plan,
    command_for_step, report_from_receipt, requested_outputs_ready, PlanValidationError,
    plan_record,
)


@pytest.fixture(autouse=True)
def deployment(tmp_path, monkeypatch):
    path = write_deployment_plan(build_module_deployment_plan(["documents"]), repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    clear_deployment_snapshot_cache()
    yield
    clear_deployment_snapshot_cache()


def template():
    source = SourceReference("source", "revision", "dotori.document.source.v1", "checksum")
    request = ProcessingRequest("request", source, ("dotori.document.extract.v1",), 200)
    step = PlanStep("extract", "parse_document", "document.extract", source.schema,
                    "dotori.document.extract.v1", {"node_id": 1})
    return static_plan(request, [step], plan_id="plan")


def validate(plan):
    return validate_plan(plan, authorize_source=lambda source: source,
                         validate_domain_step=lambda step, source: None, now=100)


def test_static_plan_uses_existing_capability_and_function_contracts():
    plan = template()
    assert validate(plan) is plan
    first = command_for_step(plan, "extract")
    assert first == command_for_step(plan, "extract")
    assert first.work_kind == "parse_document"
    assert first.deadline_at == 200
    first.payload["node_id"] = 9
    assert plan.steps[0].payload == {"node_id": 1}
    import json
    stored = json.loads(json.dumps(plan_record(plan)))
    assert stored["step_jobs"]["extract"] == first.job_id
    assert "artifact_payload" not in stored


def test_v2_existing_artifact_requires_authorized_resolver_and_round_trips():
    from orchestration.artifact_bindings import ArtifactBinding, ResolvedArtifact
    from orchestration.plans import plan_from_record
    original = template()
    binding = ArtifactBinding("artifact", "prepared-source", original.request.source.schema, "prepared-revision")
    plan = replace(original, version=2,
        steps=(replace(original.steps[0], input_binding=binding),))
    artifact = ResolvedArtifact(binding.reference, binding.schema, binding.revision,
        "source", "revision", "checksum", "policy", "ready", False)
    with pytest.raises(PlanValidationError, match="resolver"):
        validate(plan)
    assert validate_plan(plan, authorize_source=lambda source: source,
        validate_domain_step=lambda *args: None, resolve_input=lambda *args: artifact, now=100) == plan
    assert plan_from_record(plan_record(plan)) == plan
    assert "input_binding" not in plan_record(original)["steps"][0]


def test_v2_binding_cannot_bypass_dependency_or_schema_checks():
    from orchestration.artifact_bindings import ArtifactBinding
    from orchestration.plans import validate_plan_bindings
    original = template()
    for binding in (ArtifactBinding("step", "extract", original.request.source.schema),
                    ArtifactBinding("artifact", "artifact", "wrong.schema", "revision")):
        plan = replace(original, version=2, steps=(replace(original.steps[0], input_binding=binding),))
        with pytest.raises(PlanValidationError):
            validate_plan_bindings(plan)


@pytest.mark.parametrize("mutation", [
    lambda p: replace(p, version=2),
    lambda p: replace(p, request=replace(p.request, deadline_at=50)),
    lambda p: replace(p, request=replace(p.request, deadline_at=float("nan"))),
    lambda p: replace(p, request=replace(p.request, max_steps=0)),
    lambda p: replace(p, request=replace(p.request, required_schemas=("unavailable",))),
    lambda p: replace(p, steps=(p.steps[0], p.steps[0])),
    lambda p: replace(p, steps=(replace(p.steps[0], depends_on=("future",)),)),
    lambda p: replace(p, steps=(replace(p.steps[0], input_schema="wrong"),)),
    lambda p: replace(p, steps=(replace(p.steps[0], output_schema="wrong"),)),
    lambda p: replace(p, steps=(replace(p.steps[0], payload={"wrong": 1}),)),
    lambda p: replace(p, steps=(replace(p.steps[0], function_id="text.embed"),)),
])
def test_invalid_plan_rejected(mutation):
    with pytest.raises(ValueError):
        validate(mutation(template()))


def test_authority_and_domain_callbacks_cannot_be_omitted_or_bypassed():
    plan = template()
    with pytest.raises(PlanValidationError):
        validate_plan(plan, authorize_source=lambda source: replace(source, revision="new"),
                      validate_domain_step=Mock(), now=100)
    with pytest.raises(PermissionError):
        validate_plan(plan, authorize_source=Mock(side_effect=PermissionError()),
                      validate_domain_step=Mock(), now=100)
    with pytest.raises(ValueError):
        validate_plan(plan, authorize_source=lambda source: source,
                      validate_domain_step=Mock(side_effect=ValueError("payload mismatch")), now=100)


def artifact(plan):
    return {"id": "artifact", "schema": "dotori.document.extract.v1", "status": "ready", "stale": False,
            "source": {"id": plan.request.source.source_id, "revision": plan.request.source.revision,
                       "checksum": plan.request.source.checksum}}


def receipt(plan):
    return {"job_id": command_for_step(plan, "extract").job_id,
            "work_kind": "parse_document", "status": "succeeded"}


def test_report_links_receipt_and_ready_artifact_without_copying_payload():
    plan = template()
    report = report_from_receipt(plan, "extract", receipt(plan), [artifact(plan)])
    assert report.artifact_refs == ("artifact",)
    assert requested_outputs_ready(plan, [report])
    assert not requested_outputs_ready(plan, [])
    with pytest.raises(PlanValidationError):
        requested_outputs_ready(plan, [report, report])
    with pytest.raises(PlanValidationError):
        requested_outputs_ready(plan, [replace(report, plan_id="another")])


@pytest.mark.parametrize("change", [
    {"stale": True}, {"schema": "wrong"}, {"status": "failed"},
    {"source": {"id": "other", "revision": "revision"}},
])
def test_bad_artifact_cannot_complete_request(change):
    plan = template()
    with pytest.raises(PlanValidationError):
        report_from_receipt(plan, "extract", receipt(plan), [{**artifact(plan), **change}])


def test_receipt_success_without_artifact_or_wrong_job_is_not_completion():
    plan = template()
    with pytest.raises(PlanValidationError):
        report_from_receipt(plan, "extract", receipt(plan))
    with pytest.raises(PlanValidationError):
        report_from_receipt(plan, "extract", {**receipt(plan), "job_id": "wrong"}, [artifact(plan)])


@pytest.fixture
def document(db):
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from workspaces.models import Workspace, WorkspaceMembership
    from files.models import Node, FileBlob
    from documents.models import DocumentParseResult, DocumentChunk
    user = get_user_model().objects.create_user(email="plans@example.com", password="test")
    workspace = Workspace.objects.create(name="plans", created_by=user)
    membership = WorkspaceMembership.objects.create(workspace=workspace, user=user, role="admin")
    node = Node.objects.create(workspace=workspace, owner=user, name="plan.txt", ext=".txt", node_type="file")
    FileBlob.objects.create(node=node, original_name="plan.txt", mime_type="text/plain",
                           file=SimpleUploadedFile("plan.txt", b"text"), size=4, sha256="checksum", status="ready")
    result = DocumentParseResult.objects.create(node=node, status="completed", parser_name="test",
        metadata={"source_blob_uuid": str(node.blob.uuid), "source_checksum": "checksum"})
    DocumentChunk.objects.create(parse_result=result, chunk_index=0, text="text", page_from=1, section_title="section")
    return SimpleNamespace(user=user, workspace=workspace, membership=membership, node=node, result=result)


def document_plan(doc):
    from documents.processing.plan_adapter import build_document_initial_plan
    return build_document_initial_plan(actor=doc.user, workspace=doc.workspace, membership=doc.membership,
                                       source_uid=doc.node.uid)


def test_document_adapter_links_exact_job_and_authorized_result(document):
    from documents.processing.plan_adapter import document_step_report
    from documents.services.result_access import get_result
    plan = document_plan(document)
    command = command_for_step(plan, "initial-extract")
    document.result.performance_metrics = {"job_id": command.job_id}
    document.result.save(update_fields=["performance_metrics"])
    body = {"job_id": command.job_id, "work_kind": command.work_kind, "status": "succeeded"}
    report = document_step_report(plan, body, workspace=document.workspace, membership=document.membership)
    assert requested_outputs_ready(plan, [report])
    result = get_result(workspace=document.workspace, membership=document.membership, result_id=report.artifact_refs[0])
    assert result["payload"]["chunks"][0]["locator"]["page_from"] == 1
    assert result["processing"]["fingerprint"]


def test_document_old_result_cannot_satisfy_new_job(document):
    from documents.processing.plan_adapter import document_step_report
    plan = document_plan(document)
    command = command_for_step(plan, "initial-extract")
    with pytest.raises(PlanValidationError):
        document_step_report(plan, {"job_id": command.job_id, "work_kind": command.work_kind, "status": "succeeded"},
                             workspace=document.workspace, membership=document.membership)


def test_document_validation_rechecks_revision_and_membership(document):
    from documents.processing.plan_adapter import validate_document_plan
    from documents.services.result_access import ResultAccessError
    from files.models import FileBlob
    from workspaces.models import WorkspaceMembership
    plan = document_plan(document)
    FileBlob.objects.filter(node=document.node).update(sha256="changed")
    with pytest.raises(PlanValidationError):
        validate_document_plan(plan, actor=document.user, workspace=document.workspace, membership=document.membership)
    WorkspaceMembership.objects.filter(pk=document.membership.pk).update(status="pending")
    with pytest.raises(ResultAccessError):
        validate_document_plan(plan, actor=document.user, workspace=document.workspace, membership=document.membership)


def test_document_payload_cannot_target_another_node(document):
    from documents.processing.plan_adapter import validate_document_plan
    plan = document_plan(document)
    plan = replace(plan, steps=(replace(plan.steps[0], payload={"node_id": document.node.id + 1}),))
    with pytest.raises(ValueError):
        validate_document_plan(plan, actor=document.user, workspace=document.workspace, membership=document.membership)


def test_changed_deployment_requires_replanning():
    with pytest.raises(PlanValidationError, match="Deployment changed"):
        validate(replace(template(), deployment_generation="old-generation"))


def test_two_synthetic_capabilities_compose_without_document_enum(monkeypatch):
    from capabilities import functions, work_registry
    from capabilities.deployment import load_deployment_snapshot
    from orchestration import plans
    first = functions.FunctionDefinition("test.extract", 1, "test.source", "test.middle", "test", ("test-adapter",))
    second = functions.FunctionDefinition("test.convert", 1, "test.middle", "test.final", "test", ("test-adapter",))
    definitions = {f.function_id: f for f in (first, second)}
    monkeypatch.setattr(functions, "FUNCTION_REGISTRY", {**functions.FUNCTION_REGISTRY, **definitions})
    work = {kind: work_registry.WorkDefinition(kind, "test.capability", "test", "test", "test.task", ("input",))
            for kind in ("test_extract", "test_convert")}
    monkeypatch.setattr(work_registry, "WORK_REGISTRY", {**work_registry.WORK_REGISTRY, **work})
    monkeypatch.setattr(plans, "require_registered_work", lambda kind: work[kind])
    snapshot = SimpleNamespace(generation_id=load_deployment_snapshot().generation_id, role_bindings={},
        is_enabled=lambda capability: True,
        resolve_function_provider=lambda function_id, **kwargs: {
            **definitions[function_id].metadata(), "role": "test", "adapter_contract": "test-adapter"})
    source = SourceReference("source", "revision", "test.source")
    request = ProcessingRequest("request", source, ("test.final",), 200)
    plan = static_plan(request, [
        PlanStep("first", "test_extract", first.function_id, first.input_schema, first.output_schema, {"input": "source"}),
        PlanStep("second", "test_convert", second.function_id, second.input_schema, second.output_schema, {"input": "first"}, ("first",)),
    ])
    domain = Mock()
    validate_plan(plan, authorize_source=lambda value: value, validate_domain_step=domain, snapshot=snapshot, now=100)
    assert domain.call_count == 2
    reports = []
    for step in plan.steps:
        command = command_for_step(plan, step.step_id)
        item = {"id": step.step_id, "schema": step.output_schema, "status": "ready", "stale": False,
                "source": {"id": "source", "revision": "revision"}}
        reports.append(report_from_receipt(plan, step.step_id,
            {"job_id": command.job_id, "work_kind": step.work_kind, "status": "succeeded"}, [item]))
    assert not requested_outputs_ready(plan, [reports[1]])
    assert requested_outputs_ready(plan, reports)


def test_other_actor_cannot_use_admin_membership(document):
    from django.contrib.auth import get_user_model
    from documents.processing.plan_adapter import validate_document_plan
    from documents.services.result_access import ResultAccessError
    plan = document_plan(document)
    other = get_user_model().objects.create_user(email="other-plan@example.com", password="test")
    with pytest.raises(ResultAccessError):
        validate_document_plan(plan, actor=other, workspace=document.workspace, membership=document.membership)


def test_result_report_rechecks_revoked_membership(document):
    from workspaces.models import WorkspaceMembership
    from documents.processing.plan_adapter import document_step_report
    from documents.services.result_access import ResultAccessError
    plan = document_plan(document)
    command = command_for_step(plan, "initial-extract")
    WorkspaceMembership.objects.filter(pk=document.membership.pk).update(status="pending")
    with pytest.raises(ResultAccessError):
        document_step_report(plan, {"job_id": command.job_id, "work_kind": command.work_kind, "status": "running"},
                             workspace=document.workspace, membership=document.membership)
