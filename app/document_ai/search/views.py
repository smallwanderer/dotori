from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.utils.decorators import method_decorator
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.generic import TemplateView

from config.celery import app as celery_app
from drf_yasg.utils import swagger_auto_schema
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from config.enums import AIStatus, QueryAnswerMode, QueryIntent, RAGStage
from document_ai.models import RAGJob, SearchJob
from document_ai.search.serializers import (
    RAGJobCreateResponseSerializer,
    RAGJobSerializer,
    RAGRequestSerializer,
    SearchJobCreateResponseSerializer,
    SearchJobSerializer,
    VectorSearchRequestSerializer,
    VectorSearchResponseSerializer,
    VectorTuningRequestSerializer,
)
from document_ai.search.query_frontend import prepare_retrieval_query
from document_ai.parsers.text_utils import normalize_extracted_text
from document_ai.services.rag_cancel_service import set_rag_cancel_signal
from document_ai.services.llm_endpoint_service import build_rag_llm_snapshot
from document_ai.services.rag_runtime_config import (
    LLMRuntimeNotConfigured,
    server_rag_runtime_availability,
)
from document_ai.tasks import perform_vector_search
from files.models import Node, NodeType


EMPTY_SCOPE_SENTINEL = "00000000-0000-0000-0000-000000000000"


def _llm_runtime_unavailable_response(runtime_status: dict | None = None) -> Response:
    detail = runtime_status or {}
    reason_code = detail.get("reason_code") or "LLM_RUNTIME_NOT_CONFIGURED"
    if reason_code == "LLM_UNAVAILABLE_OOM":
        message = (
            "메모리 부족으로 로컬 LLM 답변 생성이 비활성화되었습니다. "
            "문서 검색은 계속 사용할 수 있습니다."
        )
    elif reason_code == "LLM_UNAVAILABLE_TIMEOUT":
        message = (
            "로컬 LLM이 제한 시간 안에 준비되지 않아 답변 생성이 비활성화되었습니다. "
            "문서 검색은 계속 사용할 수 있습니다."
        )
    else:
        message = (
            "현재 로컬 LLM을 사용할 수 없어 답변을 생성할 수 없습니다. "
            "문서 검색은 계속 사용할 수 있습니다."
        )
    return Response(
        {
            "error": {
                "code": "LLM_RUNTIME_UNAVAILABLE",
                "reason": reason_code,
                "message": message,
                "search_available": True,
                "retryable": bool(detail.get("retryable", True)),
                "recovery_command": "python3 install.py --retry-llm",
            }
        },
        status=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


def _expand_scope_node_ids(user, node_ids) -> list[str]:
    if not node_ids:
        return []

    requested_uids = [str(node_id) for node_id in node_ids]
    nodes = list(
        Node.objects.filter(
            owner=user,
            uid__in=requested_uids,
            trashed=False,
        ).only("uid", "node_type", "path")
    )
    if not nodes:
        return [EMPTY_SCOPE_SENTINEL]

    file_uids = {str(node.uid) for node in nodes if node.node_type == NodeType.FILE}
    folder_paths = [node.path.rstrip("/") for node in nodes if node.node_type == NodeType.FOLDER]

    for folder_path in folder_paths:
        descendant_uids = Node.objects.filter(
            owner=user,
            node_type=NodeType.FILE,
            trashed=False,
            path__startswith=f"{folder_path}/",
            blob__isnull=False,
        ).values_list("uid", flat=True)
        file_uids.update(str(uid) for uid in descendant_uids)

    return sorted(file_uids) or [EMPTY_SCOPE_SENTINEL]


@method_decorator(csrf_exempt, name="dispatch")
class VectorSearchView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="Vector search",
        operation_description="Queue a vector search job. Query embedding runs in a Celery worker.",
        request_body=VectorSearchRequestSerializer,
        responses={202: SearchJobCreateResponseSerializer()},
    )
    def post(self, request, *args, **kwargs):
        if not getattr(request.user, "email_verified", False):
            return Response(
                {"error": "Email verification required"},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = VectorSearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        node_ids = serializer.validated_data.get("node_ids") or []
        scoped_node_ids = _expand_scope_node_ids(request.user, node_ids)
        query_plan = prepare_retrieval_query(
            serializer.validated_data["query"],
            mode="search",
            owner=request.user,
        )
        retrieval_query = query_plan.retrieval_query or normalize_extracted_text(serializer.validated_data["query"]).strip()
        job = SearchJob.objects.create(
            owner=request.user,
            query_log=query_plan.query_log,
            query=retrieval_query,
            top_k=serializer.validated_data.get("top_k", 5),
            threshold=serializer.validated_data.get("threshold"),
            node_ids=scoped_node_ids,
        )
        async_result = perform_vector_search.apply_async(args=[job.id], queue="search")
        job.task_id = async_result.id
        job.save(update_fields=["task_id"])

        return Response(
            {
                "job_id": job.id,
                "status": job.status,
                "poll_url": request.build_absolute_uri(f"/api/document-ai/v1/search/jobs/{job.id}/"),
            },
            status=status.HTTP_202_ACCEPTED,
        )


@method_decorator(csrf_exempt, name="dispatch")
class VectorSearchJobView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="Vector search job status",
        operation_description="Return queued vector search job status and results when completed.",
        responses={200: SearchJobSerializer()},
    )
    def get(self, request, job_id: int, *args, **kwargs):
        try:
            job = SearchJob.objects.get(id=job_id, owner=request.user)
        except SearchJob.DoesNotExist:
            return Response(
                {"error": "Search job not found"},
                status=status.HTTP_404_NOT_FOUND,
            )


        return Response(SearchJobSerializer(job).data, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name="dispatch")
class VectorSandboxView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="Vector search sandbox (Tuning)",
        operation_description="Run vector search with custom tuning parameters.",
        request_body=VectorTuningRequestSerializer,
        responses={202: SearchJobCreateResponseSerializer()},
    )
    def post(self, request, *args, **kwargs):
        serializer = VectorTuningRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tuning_keys = [
            "dense_weight", "sparse_weight", "candidate_multiplier",
            "per_node_candidate_cap", "query_sparse_top_n", "evidence_top_k",
            "pool_top_k", "pool_tau", "doc_length_penalty_alpha",
            "evidence_context_window"
        ]
        tuning_params = {k: v for k, v in serializer.validated_data.items() if k in tuning_keys}

        job = SearchJob.objects.create(
            owner=request.user,
            query=serializer.validated_data["query"],
            top_k=serializer.validated_data.get("top_k", 5),
            tuning_params=tuning_params,
        )
        async_result = perform_vector_search.apply_async(args=[job.id], queue="search")
        job.task_id = async_result.id
        job.save(update_fields=["task_id"])

        return Response(
            {
                "job_id": job.id,
                "status": job.status,
                "poll_url": request.build_absolute_uri(f"/api/document-ai/v1/search/jobs/{job.id}/"),
            },
            status=status.HTTP_202_ACCEPTED,
        )


class SandboxPageView(LoginRequiredMixin, TemplateView):
    template_name = "document_ai/sandbox.html"


@method_decorator(csrf_exempt, name="dispatch")
class RAGView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="RAG answer",
        operation_description="Queue a vector search job and create a RAG answer job. \n The RAG answer generation task is queued after the search job is completed, when the job status endpoint is polled.",
        request_body=RAGRequestSerializer,
        responses={202: RAGJobCreateResponseSerializer()},
    )
    
    def post(self, request, *args, **kwargs):
        if not getattr(request.user, "email_verified", False):
            return Response(
                {"error": "Email verification required"},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = RAGRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            llm_snapshot = build_rag_llm_snapshot(request.user)
        except LLMRuntimeNotConfigured:
            return _llm_runtime_unavailable_response()

        # Explicit external endpoints do not depend on the server-managed
        # local runtime. Server-default requests are accepted only after the
        # local candidate has passed lifecycle validation.
        if not llm_snapshot.get("llm_endpoint"):
            runtime_available, runtime_status = server_rag_runtime_availability()
            if not runtime_available:
                return _llm_runtime_unavailable_response(runtime_status)

        node_ids = serializer.validated_data.get("node_ids") or []
        scoped_node_ids = _expand_scope_node_ids(request.user, node_ids)
        question = serializer.validated_data["question"]
        top_k = serializer.validated_data.get("top_k", getattr(settings, "RAG_SEARCH_TOP_K", 3))
        threshold = serializer.validated_data.get(
            "threshold",
            getattr(settings, "RAG_RETRIEVAL_THRESHOLD", None),
        )

        retrieval_query = normalize_extracted_text(question).strip()
        search_job = SearchJob.objects.create(
            owner=request.user,
            query=retrieval_query,
            top_k=top_k,
            threshold=threshold,
            node_ids=scoped_node_ids,
        )

        # RAG Job Database Record
        rag_job = RAGJob.objects.create(
            owner=request.user,
            search_job=search_job,
            question=question,
            retrieval_query=retrieval_query,
            query_intent=QueryIntent.DOCUMENT_QUESTION,
            answer_mode=QueryAnswerMode.RAG,
            retrieval_required=True,
            query_confidence=0.0,
            top_k=top_k,
            language=serializer.validated_data.get("language", "ko"),
            node_ids=[str(node_id) for node_id in node_ids],
            stage=RAGStage.SEARCHING,
            stage_message="문서에서 관련 근거를 검색하고 있습니다.",
            **llm_snapshot,
        )

        # Vector Search Worker
        async_result = perform_vector_search.apply_async(args=[search_job.id], queue="search")
        search_job.task_id = async_result.id
        search_job.save(update_fields=["task_id"])

        return Response(
            {
                "job_id": rag_job.id,
                "search_job_id": search_job.id,
                "status": rag_job.status,
                "stage": rag_job.stage,
                "stage_message": rag_job.stage_message,
                "poll_url": request.build_absolute_uri(f"/api/document-ai/v1/rag/jobs/{rag_job.id}/"),
            },
            status=status.HTTP_202_ACCEPTED,
        )


@method_decorator(csrf_exempt, name="dispatch")
class RAGJobView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="RAG job status",
        operation_description="Return RAG job status. Work is advanced by Celery workers, not by polling.",
        responses={200: RAGJobSerializer()},
    )
    def get(self, request, job_id: int, *args, **kwargs):
        try:
            rag_job = RAGJob.objects.select_related("search_job").get(id=job_id, owner=request.user)
        except RAGJob.DoesNotExist:
            return Response(
                {"error": "RAG job not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if rag_job.search_job_id:
            rag_job.search_job.refresh_from_db()
        return Response(RAGJobSerializer(rag_job).data, status=status.HTTP_200_OK)


@method_decorator(csrf_exempt, name="dispatch")
class RAGJobCancelView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    @swagger_auto_schema(
        operation_summary="Cancel RAG job",
        operation_description=(
            "Mark a RAG job as canceled and signal the worker to close the active LLM streaming connection."
        ),
        responses={200: RAGJobSerializer()},
    )
    def post(self, request, job_id: int, *args, **kwargs):
        try:
            rag_job = RAGJob.objects.select_related("search_job").get(id=job_id, owner=request.user)
        except RAGJob.DoesNotExist:
            return Response(
                {"error": "RAG job not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if rag_job.status in {AIStatus.COMPLETED, AIStatus.FAILED, AIStatus.CANCELED}:
            return Response(RAGJobSerializer(rag_job).data, status=status.HTTP_200_OK)

        now = timezone.now()
        reason = (request.data.get("reason") if isinstance(request.data, dict) else "") or "User canceled the RAG job."
        try:
            set_rag_cancel_signal(rag_job.id)
        except Exception:
            # DB state is the durable source of truth; Redis is a fast worker signal.
            pass

        if rag_job.task_id:
            celery_app.control.revoke(rag_job.task_id, terminate=False)
        if rag_job.search_job_id and rag_job.search_job.task_id:
            celery_app.control.revoke(rag_job.search_job.task_id, terminate=False)

        rag_job.status = AIStatus.CANCELED
        rag_job.stage = RAGStage.CANCELED
        rag_job.stage_message = "사용자 요청으로 RAG 작업을 중단했습니다."
        rag_job.error_message = ""
        rag_job.cancel_requested_at = rag_job.cancel_requested_at or now
        rag_job.canceled_at = now
        rag_job.cancel_reason = str(reason)[:255]
        rag_job.completed_at = now
        rag_job.save(
            update_fields=[
                "status",
                "stage",
                "stage_message",
                "error_message",
                "cancel_requested_at",
                "canceled_at",
                "cancel_reason",
                "completed_at",
                "updated_at",
            ]
        )
        if rag_job.search_job_id and rag_job.search_job.status in {AIStatus.PENDING, AIStatus.PROCESSING}:
            rag_job.search_job.status = AIStatus.CANCELED
            rag_job.search_job.completed_at = now
            rag_job.search_job.error_message = "Canceled by linked RAG job."
            rag_job.search_job.save(update_fields=["status", "completed_at", "error_message"])

        return Response(RAGJobSerializer(rag_job).data, status=status.HTTP_200_OK)
