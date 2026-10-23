import logging
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.db import transaction
from capabilities.input_resolver import InputDescriptor
from orchestration.intake import publish_initial_work
from files.models import FileBlob
from documents.orchestration import enqueue_parse
from capabilities.registry import DOCUMENT_EXTRACT
from documents.services.capability_admission import capability_enabled
from documents.services.capability_admission import CapabilityAdmissionError
from documents.tracing_utils import enqueue_kwargs
from capabilities.input_resolver import InputResolutionError

logger = logging.getLogger(__name__)


@receiver(post_save, sender=FileBlob)
def trigger_document_parsing(sender, instance, created, **kwargs):
    """Publish once, only after the uploaded blob transaction commits."""
    if created:
        transaction.on_commit(lambda: _publish_uploaded_blob(instance))


def _publish_document_initial_work(kind, payload, **kwargs):
    if kind != "parse_document":
        raise InputResolutionError("INPUT_ROUTE_MISMATCH", "Documents upload requires its registered parse route")
    return enqueue_parse(payload["node_id"], **kwargs)


def _publish_uploaded_blob(instance):
    if not capability_enabled(DOCUMENT_EXTRACT):
        logger.info(
            "New file uploaded (Node ID: %s). Document extraction capability disabled; skipping parse task.",
            instance.node_id,
        )
        return
    if not instance.node.ai_processing_enabled:
        logger.info(
            "New file uploaded (Node ID: %s). AI processing disabled; skipping parse task.",
            instance.node_id,
        )
        return
    try:
        publish_initial_work(
            InputDescriptor(instance.node_id, instance.original_name or instance.file.name, instance.mime_type or ""),
            (DOCUMENT_EXTRACT,), publisher=_publish_document_initial_work, **enqueue_kwargs(),
        )
    except (InputResolutionError, CapabilityAdmissionError) as exc:
        from documents.processing.capability_state import finalize_capability_rejection

        finalize_capability_rejection(
            "parse_document", {"node_id": instance.node_id},
            error_code=exc.code, message=str(exc),
        )
        logger.info("No initial work for node %s: %s", instance.node_id, exc)
        return
    except Exception:
        # The committed blob with no parse result is durable recovery input.
        # Do not fail the completed upload after a broker outage.
        logger.exception("Initial work publish failed for node %s; backlog recovery can retry", instance.node_id)
