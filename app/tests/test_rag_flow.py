from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.conf import settings
from django.test import TestCase, override_settings

from config.enums import AIStatus, RAGStage
from document_ai.models import LLMEndpoint, RAGJob, SearchJob, UserLLMPreference
from document_ai.services.llm_endpoint_service import check_llm_endpoint
from document_ai.search.views import EMPTY_SCOPE_SENTINEL, _expand_scope_node_ids
from document_ai.search.query_frontend import prepare_retrieval_query
from document_ai.tasks import generate_rag_response, perform_vector_search
from files.models import FileBlob, Node, NodeType

pytestmark = pytest.mark.unit

User = get_user_model()


def _search_results():
    return [
        {
            "node_id": "11111111-1111-1111-1111-111111111111",
            "node_name": "policy.pdf",
            "file_ext": ".pdf",
            "doc_score": 0.91,
            "evidences": [
                {
                    "chunk_id": 10,
                    "text": "정부는 공급 확대와 할인 지원을 병행한다.",
                    "context_text": "넓은 문맥입니다. 정부는 공급 확대와 할인 지원을 병행한다.",
                    "compressed_text": "압축 근거: 공급 확대와 할인 지원을 병행한다.",
                    "compression": {"enabled": True, "method": "embedding_lazy_segment"},
                    "section": "정책",
                    "pages": "1",
                    "distance": -0.91,
                    "dense_score": 0.8,
                    "sparse_score": 0.95,
                    "hybrid_score": 0.9,
                }
            ],
        }
    ]


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
class RAGFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="rag@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.client.force_login(self.user)

    def test_rag_request_creates_search_and_rag_jobs_then_queues_search(self):
        with patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            apply_async.return_value = SimpleNamespace(id="search-task-id")

            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "농축산물 수급 안정 대책을 요약해줘",
                    "top_k": 5,
                    "language": "ko",
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        search_job = SearchJob.objects.get(pk=payload["search_job_id"])
        rag_job = RAGJob.objects.get(pk=payload["job_id"])

        self.assertEqual(search_job.owner, self.user)
        self.assertEqual(search_job.query, "농축산물 수급 안정 대책을 요약해줘")
        self.assertEqual(search_job.top_k, 5)
        self.assertEqual(search_job.threshold, settings.RAG_RETRIEVAL_THRESHOLD)
        self.assertEqual(search_job.task_id, "search-task-id")
        self.assertEqual(rag_job.search_job, search_job)
        self.assertEqual(rag_job.status, AIStatus.PENDING)
        self.assertEqual(rag_job.stage, RAGStage.SEARCHING)
        apply_async.assert_called_once_with(args=[search_job.id], queue="search")

    def test_rag_request_snapshots_selected_user_llm_endpoint(self):
        endpoint = LLMEndpoint.objects.create(
            owner=self.user,
            name="Local Ollama",
            endpoint_type=LLMEndpoint.ENDPOINT_OLLAMA,
            base_url="http://host.docker.internal:11434/v1",
            default_model="llama3.1:8b",
        )
        UserLLMPreference.objects.create(
            user=self.user,
            rag_endpoint=endpoint,
            rag_model="qwen2.5:7b",
        )

        with patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            apply_async.return_value = SimpleNamespace(id="search-task-id")
            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "선택 모델로 답변해줘",
                    "top_k": 3,
                    "language": "ko",
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        rag_job = RAGJob.objects.get(pk=response.json()["job_id"])
        self.assertEqual(rag_job.llm_endpoint, endpoint)
        self.assertEqual(rag_job.llm_endpoint_name, "Local Ollama")
        self.assertEqual(rag_job.llm_base_url, "http://host.docker.internal:11434")
        self.assertEqual(rag_job.llm_model, "qwen2.5:7b")

    @patch.dict("os.environ", {"RAG_LLM_URL": "http://server-rag:8080/v1"})
    def test_rag_request_snapshots_selected_server_model_without_external_endpoint(self):
        UserLLMPreference.objects.create(
            user=self.user,
            rag_model="server-qwen",
        )

        with patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            apply_async.return_value = SimpleNamespace(id="search-task-id")
            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "서버 모델로 답변해줘",
                    "top_k": 3,
                    "language": "ko",
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        rag_job = RAGJob.objects.get(pk=response.json()["job_id"])
        self.assertIsNone(rag_job.llm_endpoint)
        self.assertEqual(rag_job.llm_endpoint_name, "Server default")
        self.assertEqual(rag_job.llm_base_url, "http://server-rag:8080")
        self.assertEqual(rag_job.llm_model, "server-qwen")

    def test_rag_request_accepts_explicit_retrieval_threshold(self):
        with patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            apply_async.return_value = SimpleNamespace(id="search-task-id")

            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "근거를 좁혀서 답변해줘",
                    "top_k": 3,
                    "threshold": 0.45,
                    "language": "ko",
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        search_job = SearchJob.objects.get(pk=response.json()["search_job_id"])
        self.assertEqual(search_job.top_k, 3)
        self.assertEqual(search_job.threshold, 0.45)

    def test_rag_request_expands_selected_folder_to_descendant_files(self):
        folder = Node.objects.create(
            owner=self.user,
            name="reports",
            ext="",
            node_type=NodeType.FOLDER,
        )
        file_node = Node.objects.create(
            owner=self.user,
            name="report.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=folder,
        )
        with patch("document_ai.signals.parse_document_with_docling.delay"):
            FileBlob.objects.create(
                node=file_node,
                original_name="report.txt",
                file=SimpleUploadedFile("report.txt", b"hello", content_type="text/plain"),
                mime_type="text/plain",
                size=5,
                status="ready",
            )

        with patch("document_ai.search.views.perform_vector_search.apply_async") as apply_async:
            apply_async.return_value = SimpleNamespace(id="search-task-id")
            response = self.client.post(
                "/api/document-ai/v1/rag/",
                data={
                    "question": "보고서 요약",
                    "top_k": 5,
                    "language": "ko",
                    "node_ids": [str(folder.uid)],
                },
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 202)
        search_job = SearchJob.objects.get(pk=response.json()["search_job_id"])
        self.assertEqual(search_job.node_ids, [str(file_node.uid)])

    def test_rag_poll_only_returns_status_after_search_completes(self):
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
            status=AIStatus.COMPLETED,
            results=_search_results(),
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
        )

        response = self.client.get(f"/api/document-ai/v1/rag/jobs/{rag_job.id}/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        rag_job.refresh_from_db()
        self.assertEqual(rag_job.task_id, "")
        self.assertEqual(payload["search_status"], AIStatus.COMPLETED)
        self.assertEqual(payload["search_results"][0]["node_name"], "policy.pdf")
        self.assertIn("stage", payload)
        self.assertIn("stage_message", payload)

    def test_search_completion_queues_linked_rag_generation(self):
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            stage=RAGStage.SEARCHING,
        )

        fake_retriever = SimpleNamespace(retrieve=lambda **kwargs: _search_results())
        with patch("document_ai.search.retriever.VectorRetriever", return_value=fake_retriever), patch(
            "document_ai.tasks.generate_rag_response.apply_async"
        ) as apply_async:
            apply_async.return_value = SimpleNamespace(id="rag-task-id")
            result = perform_vector_search(search_job.id)

        search_job.refresh_from_db()
        rag_job.refresh_from_db()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["queued_rag_jobs"], 1)
        self.assertEqual(search_job.status, AIStatus.COMPLETED)
        self.assertEqual(rag_job.task_id, "rag-task-id")
        self.assertEqual(rag_job.stage, RAGStage.GENERATING)
        self.assertIn("답변", rag_job.stage_message)
        apply_async.assert_called_once_with(args=[rag_job.id], queue="rag")

    def test_generate_rag_response_uses_search_evidence_and_stores_citations(self):
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
            status=AIStatus.COMPLETED,
            results=_search_results(),
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            language="ko",
        )

        class FakeResponse:
            status_code = 200
            text = ""

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=False):
                chunks = [
                    'data: {"choices":[{"delta":{"content":"<|channel>final\\n핵심 답변\\n"}}]}',
                    'data: {"choices":[{"delta":{"content":"- 공급 확대와 할인 지원을 병행합니다 [1].\\n\\n"}}]}',
                    'data: {"choices":[{"delta":{"content":"주요 근거\\n- policy.pdf / section 정책 / page 1 [1]"}}]}',
                    "data: [DONE]",
                ]
                return chunks

        fake_semaphore = SimpleNamespace(acquire=lambda timeout=None: None, release=lambda: None)

        with patch("requests.post", return_value=FakeResponse()) as post, patch(
            "redis.Redis.from_url", return_value=object()
        ), patch("redis_semaphore.Semaphore", return_value=fake_semaphore):
            result = generate_rag_response(rag_job.id)

        rag_job.refresh_from_db()
        self.assertEqual(result["status"], "success")
        self.assertEqual(rag_job.status, AIStatus.COMPLETED)
        self.assertIn("공급 확대", rag_job.answer)
        self.assertEqual(len(rag_job.citations), 1)
        self.assertEqual(rag_job.citations[0]["node_name"], "policy.pdf")
        self.assertEqual(rag_job.citations[0]["text"], "압축 근거: 공급 확대와 할인 지원을 병행한다.")
        post.assert_called_once()
        request_payload = post.call_args.kwargs["json"]
        final_user_prompt = request_payload["messages"][-1]["content"]
        self.assertTrue(request_payload["stream"])
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertIn("압축 근거", final_user_prompt)
        self.assertNotIn("넓은 문맥입니다", final_user_prompt)

    def test_generate_rag_response_uses_snapshotted_llm_endpoint_and_model(self):
        endpoint = LLMEndpoint.objects.create(
            owner=self.user,
            name="Local llama.cpp",
            endpoint_type=LLMEndpoint.ENDPOINT_LLAMA_CPP,
            base_url="http://llm-runtime:8080",
            default_model="gemma-local",
            api_key="local-secret",
        )
        search_job = SearchJob.objects.create(
            owner=self.user,
            query="대책 요약",
            top_k=3,
            status=AIStatus.COMPLETED,
            results=_search_results(),
        )
        rag_job = RAGJob.objects.create(
            owner=self.user,
            search_job=search_job,
            question="대책 요약",
            top_k=3,
            language="ko",
            llm_endpoint=endpoint,
            llm_endpoint_name=endpoint.name,
            llm_base_url=endpoint.normalized_base_url,
            llm_model="gemma-selected",
        )

        class FakeResponse:
            status_code = 200
            text = ""

            def raise_for_status(self):
                return None

            def iter_lines(self, decode_unicode=False):
                return [
                    'data: {"choices":[{"delta":{"content":"공급 확대와 "}}]}',
                    'data: {"choices":[{"delta":{"content":"할인 지원입니다 [1]."}}]}',
                    "data: [DONE]",
                ]

        fake_semaphore = SimpleNamespace(acquire=lambda timeout=None: None, release=lambda: None)

        with patch("requests.post", return_value=FakeResponse()) as post, patch(
            "redis.Redis.from_url", return_value=object()
        ), patch("redis_semaphore.Semaphore", return_value=fake_semaphore):
            result = generate_rag_response(rag_job.id)

        self.assertEqual(result["status"], "success")
        self.assertEqual(post.call_args.args[0], "http://llm-runtime:8080/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "gemma-selected")
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer local-secret")

    def test_llm_endpoint_healthcheck_marks_endpoint_available(self):
        endpoint = LLMEndpoint.objects.create(
            owner=self.user,
            name="OpenAI compatible",
            endpoint_type=LLMEndpoint.ENDPOINT_OPENAI_COMPATIBLE,
            base_url="https://api.example.test/v1",
            default_model="qwen-test",
            api_key="secret-key",
        )

        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {"data": [{"id": "qwen-test"}]}

        with patch("requests.get", return_value=FakeResponse()) as get:
            checked = check_llm_endpoint(owner=self.user, endpoint_id=str(endpoint.id))

        checked.refresh_from_db()
        self.assertEqual(checked.last_check_status, "ok")
        self.assertIn("기본 모델", checked.last_check_message)
        self.assertEqual(get.call_args.args[0], "https://api.example.test/v1/models")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer secret-key")

    def test_settings_create_llm_endpoint_runs_healthcheck(self):
        class FakeResponse:
            status_code = 200
            text = ""

            def json(self):
                return {"data": [{"id": "qwen-test"}]}

        with patch("requests.get", return_value=FakeResponse()) as get:
            response = self.client.post(
                "/accounts/settings/",
                data={
                    "action": "create_llm_endpoint",
                    "endpoint_name": "External API",
                    "endpoint_type": LLMEndpoint.ENDPOINT_OPENAI_COMPATIBLE,
                    "base_url": "https://api.example.test/v1",
                    "default_model": "qwen-test",
                    "api_key": "secret-key",
                },
            )

        self.assertEqual(response.status_code, 302)
        endpoint = LLMEndpoint.objects.get(owner=self.user, name="External API")
        self.assertEqual(endpoint.last_check_status, "ok")
        self.assertEqual(get.call_args.args[0], "https://api.example.test/v1/models")

    def test_llm_endpoint_healthcheck_marks_endpoint_failed(self):
        endpoint = LLMEndpoint.objects.create(
            owner=self.user,
            name="Broken endpoint",
            endpoint_type=LLMEndpoint.ENDPOINT_OPENAI_COMPATIBLE,
            base_url="https://broken.example.test",
            default_model="qwen-test",
        )

        with patch("requests.get", side_effect=requests.RequestException("connection failed")):
            checked = check_llm_endpoint(owner=self.user, endpoint_id=str(endpoint.id))

        checked.refresh_from_db()
        self.assertEqual(checked.last_check_status, "failed")
        self.assertIn("connection failed", checked.last_check_message)

    def test_query_frontend_keeps_querydsl_experimental_and_passthrough(self):
        plan = prepare_retrieval_query("지난주 pdf 계약 문서", mode="rag")

        self.assertEqual(plan.source, "passthrough")
        self.assertEqual(plan.retrieval_query, "지난주 pdf 계약 문서")
        self.assertEqual(plan.metadata, {})

        with patch.dict("os.environ", {"QUERY_FRONTEND_MODE": "experimental_querydsl"}):
            experimental_plan = prepare_retrieval_query("지난주 pdf 계약 문서", mode="rag")

        self.assertEqual(experimental_plan.source, "querydsl_experimental_passthrough")
        self.assertEqual(experimental_plan.retrieval_query, "지난주 pdf 계약 문서")
        self.assertFalse(experimental_plan.metadata["querydsl_enabled"])

    def test_scope_expansion_includes_files_under_selected_folder(self):
        folder = Node.objects.create(
            owner=self.user,
            name="reports",
            ext="",
            node_type=NodeType.FOLDER,
        )
        file_node = Node.objects.create(
            owner=self.user,
            name="report.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            parent=folder,
        )
        with patch("document_ai.signals.parse_document_with_docling.delay"):
            FileBlob.objects.create(
                node=file_node,
                original_name="report.txt",
                file=SimpleUploadedFile("report.txt", b"hello", content_type="text/plain"),
                mime_type="text/plain",
                size=5,
                status="ready",
            )

        scoped_ids = _expand_scope_node_ids(self.user, [folder.uid])

        self.assertEqual(scoped_ids, [str(file_node.uid)])

    def test_scope_expansion_uses_sentinel_when_selection_has_no_files(self):
        empty_folder = Node.objects.create(
            owner=self.user,
            name="empty",
            ext="",
            node_type=NodeType.FOLDER,
        )

        scoped_ids = _expand_scope_node_ids(self.user, [empty_folder.uid])

        self.assertEqual(scoped_ids, [EMPTY_SCOPE_SENTINEL])
