"""Documents compiler and execution admission; common control owns delivery."""
from documents.processing.embedding_contract import (
    EmbeddingIntent, EmbeddingDelivery, ParseBinding, parse_embedding_intent,
    parse_embedding_delivery, EmbeddingContractError,
)
from documents.processing.embedding_preparation import (
    _source_context, _resolved_parse, resolve_document_artifact,
)
from documents.processing.embedding_manifests import approve_embedding_manifest
from documents.services.plan_delivery import _context
from orchestration.plans import command_for_step


def build_document_embedding_plan(**request):
    """Compatibility facade; common orchestration owns selection/composition."""
    from orchestration.preparation import prepare_bound_plan
    return prepare_bound_plan(DocumentEmbeddingPreparationAdapter(**request))


def submit_document_embedding_plan(**request):
    """Explicit execution intake; never promotes an existing preparation row."""
    from orchestration.submissions import submit_plan
    plan = build_document_embedding_plan(**request)
    return submit_plan(plan, actor=request["actor"], workspace=request["workspace"])


class DocumentEmbeddingPreparationAdapter:
    """Input evidence and request description only; never returns plan steps."""
    def __init__(self, *, actor, workspace, membership, source_uid, request_id,
                 selector=None, mode="missing", pinned_input=None, deadline_at=None):
        self.context = dict(actor=actor, workspace=workspace, membership=membership)
        self.source_uid = source_uid
        self.request_id = request_id
        self.selector = {"type": "all"} if selector is None else selector
        self.mode, self.pinned_input, self.deadline_at = mode, pinned_input, deadline_at

    def validate_request(self):
        from documents.services.embedding_runtime_config import load_embedding_runtime
        from documents.processing.embedding_contract import INTENT_SCHEMA
        if not isinstance(self.request_id, str) or not 0 < len(self.request_id) <= 64:
            raise ValueError("request_id must be a non-empty bounded string")
        node, _, source = _source_context(source_uid=self.source_uid, **self.context)
        runtime = load_embedding_runtime()
        intent = parse_embedding_intent({"schema": INTENT_SCHEMA,
            "source": {"workspace_id": str(self.context["workspace"].pk), "uid": source.source_id,
                       "revision": source.revision, "checksum": source.checksum},
            "selector": self.selector, "mode": self.mode,
            "runtime": {"generation_id": runtime.generation_id, "fingerprint": runtime.runtime_fingerprint}})
        return node, source, intent

    def inspect_input(self, context):
        from orchestration.preparation import InputReadiness
        try:
            _, artifact, ids = _resolved_parse(context[0], context[1])
        except EmbeddingContractError as exc:
            if exc.code not in {"PARSE_MISSING", "PARSE_NOT_READY", "PARSE_POLICY_CHANGED", "PARSE_CONTENT_CHANGED"}:
                raise
            return InputReadiness(False, exc.code, exc)
        pinned = ParseBinding(artifact.artifact_id, artifact.schema, artifact.revision,
                              artifact.processing_fingerprint)
        if self.pinned_input is not None and self.pinned_input != pinned:
            raise EmbeddingContractError("PARSE_NOT_READY", "Pinned extraction changed")
        intent = context[2]
        if intent.selector.type == "chunk_ids" and not set(intent.selector.ids).issubset(ids):
            raise EmbeddingContractError("CHUNK_OUTSIDE_ARTIFACT", "Selected chunks are unavailable")
        return InputReadiness(True, "VALID_PARSE", artifact)

    def require_prerequisite_allowed(self, context, evidence):
        if self.pinned_input is not None or context[2].selector.type != "all":
            raise evidence.value

    def describe_request(self, context, evidence):
        from orchestration.artifact_bindings import ArtifactBinding
        from orchestration.preparation import BoundPlanDescription
        binding = None
        if evidence.ready:
            artifact = evidence.value
            binding = ArtifactBinding("artifact", artifact.artifact_id, artifact.schema, artifact.revision)
        return BoundPlanDescription(source=context[1], request_id=self.request_id,
            work_kind="embed_document", function_id="document.embed", intent=context[2].to_payload(),
            reusable_binding=binding, prerequisite_work_kind="parse_document",
            prerequisite_function_id="document.extract", prerequisite_payload={"node_id": context[0].pk},
            prerequisite_step_id="initial-extract", deadline_at=self.deadline_at)


class DocumentEmbeddingCompiler:
    """Compiler ports called after common admission and dependency checks."""
    def resolve_input(self, plan, row, binding, *, producer_job_id=None):
        return resolve_document_artifact(binding, source_uid=plan.request.source.source_id,
            producer_job_id=producer_job_id, **_context(row))

    def compile_command(self, plan, row, step, artifact):
        intent = parse_embedding_intent(step.payload)
        command = intent.bind(ParseBinding(artifact.artifact_id, artifact.schema,
                                          artifact.revision, artifact.processing_fingerprint))
        manifest = approve_embedding_manifest(plan_id=row.pk, step_id=step.step_id, command=command)
        return EmbeddingDelivery(command, str(manifest.pk), manifest.fingerprint).to_payload()

    def validate_compiled_command(self, plan, row, step, payload, artifact):
        from documents.models import DocumentEmbeddingManifest
        delivery = parse_embedding_delivery(payload)
        expected_input = ParseBinding(artifact.artifact_id, artifact.schema,
                                      artifact.revision, artifact.processing_fingerprint)
        if delivery.command.input != expected_input:
            raise EmbeddingContractError("MANIFEST_MISMATCH", "Compiled artifact changed")
        # Publication validation retains current parser policy and never selects
        # again when a manifest exists. Missing reference must not create a row.
        stored = DocumentEmbeddingManifest.objects.filter(pk=delivery.manifest_id,
            plan_id=row.pk, step_id=step.step_id,
            job_id=command_for_step(plan, step.step_id).job_id).first()
        if stored is None or stored.fingerprint != delivery.manifest_fingerprint:
            raise EmbeddingContractError("MANIFEST_MISMATCH", "Unapproved manifest reference")
        approved = approve_embedding_manifest(plan_id=row.pk, step_id=step.step_id,
                                              command=delivery.command)
        if approved.pk != stored.pk:
            raise EmbeddingContractError("MANIFEST_MISMATCH", "Manifest identity changed")


class DocumentEmbeddingPlanAdapter(DocumentEmbeddingCompiler):
    """Separate preparation and installed-route execution admission."""
    def source_guard(self, plan, row):
        from documents.services.plan_delivery import DOMAIN_ADAPTER
        return DOMAIN_ADAPTER.source_guard(plan, row)

    def validate_preparation(self, plan, row):
        return self._validate(plan, row, preparation=True)

    def _validate(self, plan, row, *, preparation):
        from documents.processing.plan_adapter import validate_document_plan
        from documents.services.embedding_runtime_config import load_embedding_runtime
        from orchestration.contracts import WorkContractError
        from documents.services.result_access import ResultAccessError
        from capabilities.deployment import load_deployment_snapshot
        try:
            validate_document_plan(plan, preparation=preparation, work_kind="parse_document", **_context(row))
            node, _, source = _source_context(source_uid=row.source_uid, **_context(row))
            runtime = load_embedding_runtime()
            snapshot = load_deployment_snapshot()
            installation_id = f"embedding:{runtime.generation_id}"
            provider = snapshot.resolve_function_provider("text.embed",
                explicit_model_installation_id=installation_id)
            installed = snapshot.installed_models.get(installation_id, {})
            if (provider.get("model_installation_id") != installation_id
                    or snapshot.role_bindings.get("embedding", {}).get("model_installation_id") != installation_id
                    or installed.get("artifact_revision") != (
                        getattr(runtime, "model_revision", "") or runtime.runtime_fingerprint or runtime.generation_id)):
                raise WorkContractError("Selected text.embed differs from the server runtime")
            for step in plan.steps:
                if step.work_kind != "embed_document":
                    continue
                intent = parse_embedding_intent(step.payload)
                if (intent.source.uid != source.source_id or intent.source.revision != source.revision
                        or intent.source.checksum != source.checksum
                        or intent.source.workspace_id != str(row.workspace_id)
                        or intent.runtime.generation_id != runtime.generation_id
                        or intent.runtime.fingerprint != runtime.runtime_fingerprint):
                    raise WorkContractError("Embedding intent source/runtime changed")
        except (ValueError, ResultAccessError) as exc:
            raise WorkContractError(str(exc)) from exc

    def validate(self, plan, row):
        return self._validate(plan, row, preparation=False)

    def resolve_input(self, plan, row, binding, *, producer_job_id=None):
        from documents.services.result_access import ResultAccessError
        from orchestration.contracts import WorkContractError
        try:
            return super().resolve_input(plan, row, binding, producer_job_id=producer_job_id)
        except ResultAccessError as exc:
            raise WorkContractError(str(exc)) from exc

    def report(self, plan, row, response):
        from documents.processing.embedding_reports import embedding_step_report
        return embedding_step_report(plan, row, response)


EMBEDDING_DOMAIN_ADAPTER = DocumentEmbeddingPlanAdapter()
