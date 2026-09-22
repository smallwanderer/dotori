import logging
from django.db.models.signals import post_save
from django.dispatch import receiver
from files.models import FileBlob
from document_ai.orchestration import enqueue_parse
from capabilities.registry import DOCUMENT_EXTRACT
from document_ai.services.capability_admission import capability_enabled
from document_ai.services.capability_admission import CapabilityAdmissionError
from document_ai.tracing_utils import enqueue_kwargs
from capabilities.input_resolver import InputResolutionError
from document_ai.processing.input_routing import resolve_blob_work

logger = logging.getLogger(__name__)


@receiver(post_save, sender=FileBlob)
def trigger_document_parsing(sender, instance, created, **kwargs):
    """
    FileBlob(파일 인스턴스)가 새로 생성되었을 때, 
    자동으로 파싱(및 이어서 임베딩)을 수행하는 Celery 태스크를 호출합니다.
    """
    if created:
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
            work = resolve_blob_work(instance, (DOCUMENT_EXTRACT,))
        except InputResolutionError as exc:
            from document_ai.processing.capability_state import finalize_capability_rejection

            finalize_capability_rejection(
                "parse_document", {"node_id": instance.node_id},
                error_code=exc.code, message=str(exc),
            )
            logger.info("No initial work for node %s: %s", instance.node_id, exc)
            return
        if not work:
            return
        logger.info("New file uploaded (Node ID: %s). Triggering parse task.", instance.node_id)
        # 파싱 태스크 비동기 호출! 파싱이 끝나면 내부적으로 임베딩 큐잉 태스크를 또 부르게 됩니다.
        # 업로드 요청 스레드에서 발생하므로 여기서 얻는 trace_id가 "적재 trace"의 시작점이 됩니다.
        try:
            enqueue_parse(work[0].payload["node_id"], **enqueue_kwargs())
        except CapabilityAdmissionError as exc:
            from document_ai.processing.capability_state import finalize_capability_rejection

            finalize_capability_rejection(
                "parse_document", {"node_id": instance.node_id},
                error_code=exc.code, message=str(exc),
            )
            logger.warning("Parse admission rejected for node %s: %s", instance.node_id, exc)
