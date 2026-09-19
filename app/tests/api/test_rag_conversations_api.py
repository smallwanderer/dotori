import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from asgiref.sync import async_to_sync

from config.enums import AIStatus, NodeType
from document_ai.models import RAGConversation, RAGJob, RAGMessage
from document_ai.rag.async_generation import (
    AsyncGenerationContext,
    _complete_generation_sync,
    _mark_generation_canceled_sync,
)
from document_ai.rag.streaming import _create_rag_jobs_sync
from document_ai.services.rag_conversation_service import (
    ConversationIdempotencyConflict,
    append_user_message,
    fail_reserved_rag_job,
    recover_expired_rag_jobs,
    reserve_session_turn,
    renew_rag_job_lease,
)
from files.models import Node
from workspaces.models import WorkspaceMembership
from workspaces.services import create_team_workspace


User = get_user_model()


class RAGConversationApiTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            email="conversation-owner@example.com", password="test-pass",
            email_verified=True, is_active=True,
        )
        self.member = User.objects.create_user(
            email="conversation-member@example.com", password="test-pass",
            email_verified=True, is_active=True,
        )
        self.workspace = create_team_workspace(actor=self.owner, name="Conversation Team")
        WorkspaceMembership.objects.create(
            workspace=self.workspace, user=self.member,
            role=WorkspaceMembership.ROLE_MEMBER, status=WorkspaceMembership.STATUS_ACTIVE,
        )

    def _login(self, user):
        self.client.force_login(user)
        response = self.client.post(
            "/api/workspaces/v1/switch/",
            data=json.dumps({"workspace_uid": str(self.workspace.uid)}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

    def test_workspace_members_can_read_but_only_owner_or_admin_can_edit(self):
        self._login(self.owner)
        created = self.client.post(
            "/api/document-ai/v1/rag/conversations/",
            data=json.dumps({"title": "계약 검토", "default_node_ids": []}),
            content_type="application/json",
        )
        self.assertEqual(created.status_code, 201)
        conversation = created.json()["conversation"]
        self.assertEqual(conversation["title"], "계약 검토")
        self.assertTrue(conversation["can_manage"])

        self.client.logout()
        self._login(self.member)
        listed = self.client.get("/api/document-ai/v1/rag/conversations/")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item["uid"] for item in listed.json()["conversations"]], [conversation["uid"]])
        self.assertFalse(listed.json()["conversations"][0]["can_manage"])
        forbidden = self.client.patch(
            f"/api/document-ai/v1/rag/conversations/{conversation['uid']}/",
            data=json.dumps({"title": "수정", "expected_revision": 1}),
            content_type="application/json",
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_message_order_and_idempotency_are_persisted(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="질문"
        )
        request_id = uuid.uuid4()
        first, created = append_user_message(
            conversation=conversation, user=self.owner, content="첫 질문", client_request_id=request_id,
            node_ids=[],
        )
        replay, replay_created = append_user_message(
            conversation=conversation, user=self.owner, content="첫 질문", client_request_id=request_id,
            node_ids=[],
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first.pk, replay.pk)
        self.assertEqual(first.sequence, 1)
        self.assertEqual(RAGMessage.objects.filter(conversation=conversation).count(), 1)

        with self.assertRaises(ConversationIdempotencyConflict):
            append_user_message(
                conversation=conversation, user=self.owner, content="다른 질문",
                client_request_id=request_id, node_ids=[],
            )

    def test_default_scope_requires_live_nodes_in_the_current_workspace(self):
        self._login(self.owner)
        node = Node.objects.create(
            owner=self.owner, workspace=self.workspace, name="scope", ext="",
            node_type=NodeType.FOLDER, path="/scope",
        )
        valid = self.client.post(
            "/api/document-ai/v1/rag/conversations/",
            data=json.dumps({"default_node_ids": [str(node.uid)]}),
            content_type="application/json",
        )
        self.assertEqual(valid.status_code, 201)
        invalid = self.client.post(
            "/api/document-ai/v1/rag/conversations/",
            data=json.dumps({"default_node_ids": ["not-a-uuid", str(uuid.uuid4())]}),
            content_type="application/json",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_DOCUMENT_SCOPE")

    def test_revision_update_rejects_stale_writes(self):
        self._login(self.owner)
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="v1"
        )
        path = f"/api/document-ai/v1/rag/conversations/{conversation.uid}/"
        first = self.client.patch(
            path, data=json.dumps({"title": "v2", "expected_revision": 1}),
            content_type="application/json",
        )
        stale = self.client.patch(
            path, data=json.dumps({"title": "stale", "expected_revision": 1}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["conversation"]["revision"], 2)
        self.assertEqual(stale.status_code, 409)
        conversation.refresh_from_db()
        self.assertEqual(conversation.title, "v2")

    def test_delete_hides_conversation_and_rejects_active_generation(self):
        self._login(self.owner)
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="삭제 대상"
        )
        path = f"/api/document-ai/v1/rag/conversations/{conversation.uid}/"

        RAGJob.objects.create(
            workspace=self.workspace, owner=self.owner, conversation=conversation,
            question="진행 중 질문", status=AIStatus.PROCESSING,
        )
        busy = self.client.delete(path)
        self.assertEqual(busy.status_code, 409)
        self.assertEqual(busy.json()["error"]["code"], "CONVERSATION_BUSY")

        conversation.rag_jobs.update(status=AIStatus.COMPLETED)
        deleted = self.client.delete(path)
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(
            self.client.get("/api/document-ai/v1/rag/conversations/").json()["conversations"],
            [],
        )

    def test_cancel_signals_the_active_job_and_is_safe_without_one(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="중단 대상"
        )
        rag_job = RAGJob.objects.create(
            workspace=self.workspace, owner=self.owner, conversation=conversation,
            question="진행 중 질문", status=AIStatus.PROCESSING,
        )
        self._login(self.owner)
        path = f"/api/document-ai/v1/rag/conversations/{conversation.uid}/cancel/"

        with patch(
            "document_ai.services.rag_conversation_service.set_rag_cancel_signal",
            return_value=True,
        ) as cancel_signal:
            requested = self.client.post(path)
        self.assertEqual(requested.status_code, 200)
        self.assertTrue(requested.json()["cancel_requested"])
        self.assertEqual(requested.json()["job_id"], rag_job.id)
        cancel_signal.assert_called_once_with(rag_job.id)
        rag_job.refresh_from_db()
        self.assertIsNotNone(rag_job.cancel_requested_at)

        rag_job.status = AIStatus.CANCELED
        rag_job.save(update_fields=["status", "updated_at"])
        no_active_job = self.client.post(path)
        self.assertEqual(no_active_job.status_code, 200)
        self.assertFalse(no_active_job.json()["cancel_requested"])
        self.assertIsNone(no_active_job.json()["job_id"])

    def test_cancel_requested_job_stays_delete_protected_until_terminal(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="중단 후 삭제"
        )
        rag_job = RAGJob.objects.create(
            workspace=self.workspace, owner=self.owner, conversation=conversation,
            question="취소 대기 질문", status=AIStatus.PROCESSING,
        )
        self._login(self.owner)
        cancel_path = f"/api/document-ai/v1/rag/conversations/{conversation.uid}/cancel/"
        delete_path = f"/api/document-ai/v1/rag/conversations/{conversation.uid}/"

        with patch("document_ai.services.rag_conversation_service.set_rag_cancel_signal"):
            requested = self.client.post(cancel_path)
        self.assertEqual(requested.status_code, 200)
        still_busy = self.client.delete(delete_path)
        self.assertEqual(still_busy.status_code, 409)
        self.assertEqual(still_busy.json()["error"]["code"], "CONVERSATION_BUSY")

        rag_job.status = AIStatus.CANCELED
        rag_job.save(update_fields=["status", "updated_at"])
        deleted = self.client.delete(delete_path)
        self.assertEqual(deleted.status_code, 204)

    def test_session_stream_reports_idempotency_conflict_separately(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        request_id = uuid.uuid4()
        append_user_message(
            conversation=conversation, user=self.owner, content="원래 질문",
            client_request_id=request_id, node_ids=[],
        )
        self._login(self.owner)
        with patch(
            "document_ai.search.views.build_rag_llm_snapshot", return_value={"llm_model": "test"}
        ), patch(
            "document_ai.search.views.server_rag_runtime_availability", return_value=(True, {})
        ), patch(
            "document_ai.search.views.acquire_rag_admission_token_async",
            new=AsyncMock(),
        ) as acquire_rag_admission_token_async:
            response = self.client.post(
                f"/api/document-ai/v1/rag/conversations/{conversation.uid}/messages/stream/",
                data=json.dumps({
                    "question": "다른 질문", "language": "ko",
                    "client_request_id": str(request_id),
                }),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "IDEMPOTENCY_CONFLICT")
        acquire_rag_admission_token_async.assert_not_awaited()

    def test_session_stream_replays_an_existing_completed_turn_without_search(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        request_id = uuid.uuid4()
        user_message, _ = append_user_message(
            conversation=conversation, user=self.owner, content="기존 질문",
            client_request_id=request_id, node_ids=[],
        )
        rag_job = RAGJob.objects.create(
            workspace=self.workspace, owner=self.owner, conversation=conversation,
            question="기존 질문", status=AIStatus.COMPLETED,
            answer="기존 답변", citations=[{"id": 1, "node_name": "문서"}],
            completed_at=timezone.now(),
        )
        RAGMessage.objects.create(
            conversation=conversation, sequence=2, role=RAGMessage.ROLE_ASSISTANT,
            reply_to=user_message, rag_job=rag_job, node_ids=[],
        )
        self._login(self.owner)
        with patch(
            "document_ai.search.views.build_rag_llm_snapshot", return_value={"llm_model": "test"}
        ), patch(
            "document_ai.search.views.server_rag_runtime_availability", return_value=(True, {})
        ), patch(
            "document_ai.search.views.acquire_rag_admission_token_async",
            new=AsyncMock(),
        ) as acquire_rag_admission_token_async, patch(
            "document_ai.search.execution.perform_vector_search_sync"
        ) as vector_search:
            response = self.client.post(
                f"/api/document-ai/v1/rag/conversations/{conversation.uid}/messages/stream/",
                data=json.dumps({
                    "question": "기존 질문", "language": "ko",
                    "client_request_id": str(request_id),
                }),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["replay"])
        self.assertEqual(payload["status"], AIStatus.COMPLETED)
        self.assertEqual(payload["answer"], "기존 답변")
        vector_search.assert_not_called()
        acquire_rag_admission_token_async.assert_not_awaited()

    def test_session_stream_rejects_a_second_active_answer_without_leaving_a_message(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        active_job = RAGJob.objects.create(
            workspace=self.workspace, owner=self.owner, conversation=conversation,
            question="먼저 시작한 질문", status=AIStatus.PROCESSING,
        )
        self._login(self.owner)
        admission_token = SimpleNamespace(release=Mock(), release_async=AsyncMock())
        with patch(
            "document_ai.search.views.build_rag_llm_snapshot", return_value={"llm_model": "test"}
        ), patch(
            "document_ai.search.views.server_rag_runtime_availability", return_value=(True, {})
        ), patch(
            "document_ai.search.views.acquire_rag_admission_token_async",
            new=AsyncMock(return_value=admission_token),
        ), patch(
            "document_ai.search.execution.perform_vector_search_sync"
        ) as vector_search:
            response = self.client.post(
                f"/api/document-ai/v1/rag/conversations/{conversation.uid}/messages/stream/",
                data=json.dumps({
                    "question": "동시에 보낸 질문", "language": "ko",
                    "client_request_id": str(uuid.uuid4()),
                }),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "CONVERSATION_BUSY")
        self.assertEqual(list(conversation.rag_jobs.values_list("id", flat=True)), [active_job.id])
        self.assertFalse(conversation.messages.exists())
        vector_search.assert_not_called()
        admission_token.release_async.assert_not_awaited()

    def test_rag_job_rejects_a_conversation_from_another_workspace(self):
        other_workspace = create_team_workspace(actor=self.owner, name="Other Team")
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        with self.assertRaisesMessage(ValueError, "same workspace"):
            RAGJob.objects.create(
                workspace=other_workspace, owner=self.owner,
                conversation=conversation, question="잘못된 연결",
            )

    def test_session_job_and_assistant_message_are_atomic_and_restorable(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="세션"
        )
        user_message, _ = append_user_message(
            conversation=conversation, user=self.owner, content="질문",
            client_request_id=uuid.uuid4(), node_ids=[],
        )
        with patch(
            "document_ai.search.execution.perform_vector_search_sync",
            return_value={"status": "success"},
        ):
            _, rag_job = _create_rag_jobs_sync(
                owner=self.owner, workspace=self.workspace, question="질문",
                retrieval_query="질문", top_k=3, threshold=None, language="ko",
                requested_node_ids=[], scoped_node_ids=[], llm_snapshot={},
                conversation=conversation, reply_to=user_message,
            )
        self.assertEqual(rag_job.conversation_id, conversation.id)
        assistant = RAGMessage.objects.get(rag_job=rag_job)
        self.assertEqual(assistant.reply_to_id, user_message.id)
        self.assertEqual(assistant.sequence, 2)

        rag_job.status = AIStatus.COMPLETED
        rag_job.answer = "저장된 답변"
        rag_job.citations = [{"id": 1, "text": "근거"}]
        rag_job.save(update_fields=["status", "answer", "citations", "updated_at"])
        self._login(self.owner)
        restored = self.client.get(
            f"/api/document-ai/v1/rag/conversations/{conversation.uid}/messages/"
        )
        self.assertEqual(restored.status_code, 200)
        messages = restored.json()["messages"]
        self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
        self.assertEqual(messages[1]["rag_job"]["answer"], "저장된 답변")

    def test_session_turn_is_reserved_before_admission_and_can_be_failed(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        user_message, rag_job, created = reserve_session_turn(
            conversation=conversation, user=self.owner, question="예약 질문",
            client_request_id=uuid.uuid4(), node_ids=[], retrieval_query="예약 질문",
            top_k=3, threshold=None, language="ko", llm_snapshot={}
        )
        self.assertTrue(created)
        self.assertEqual(rag_job.status, AIStatus.PENDING)
        self.assertTrue(RAGMessage.objects.filter(reply_to=user_message, rag_job=rag_job).exists())
        self.assertEqual(fail_reserved_rag_job(rag_job_id=rag_job.id, error="capacity"), 1)
        rag_job.refresh_from_db()
        self.assertEqual(rag_job.status, AIStatus.FAILED)
        self.assertEqual(rag_job.error_message, "capacity")

    def test_session_stream_endpoint_links_and_exposes_message_identity(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="실제 스트림"
        )
        self._login(self.owner)
        request_id = uuid.uuid4()
        admission_token = SimpleNamespace(release=Mock(), release_async=AsyncMock())

        async def generation_events(_job_id):
            yield {"type": "terminal", "result": {"status": "success"}}

        with patch(
            "document_ai.search.views.build_rag_llm_snapshot", return_value={"llm_model": "test"}
        ), patch(
            "document_ai.search.views.server_rag_runtime_availability", return_value=(True, {})
        ), patch(
            "document_ai.search.views.acquire_rag_admission_token_async",
            new=AsyncMock(return_value=admission_token),
        ), patch(
            "document_ai.search.execution.perform_vector_search_sync",
            return_value={"status": "success"},
        ), patch(
            "document_ai.rag.async_generation.iter_rag_generation_events_async",
            side_effect=generation_events,
        ):
            response = self.client.post(
                f"/api/document-ai/v1/rag/conversations/{conversation.uid}/messages/stream/",
                data=json.dumps({
                    "question": "세션 질문", "language": "ko",
                    "client_request_id": str(request_id),
                }),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)

            async def collect_events():
                return [json.loads(chunk.decode("utf-8")) async for chunk in response.streaming_content]

            events = async_to_sync(collect_events)()

        self.assertEqual([event["type"] for event in events], ["started", "sources", "completed"])
        self.assertEqual(events[0]["conversation_uid"], str(conversation.uid))
        self.assertEqual(events[0]["client_request_id"], str(request_id))
        self.assertTrue(events[0]["message_uid"])
        messages = list(RAGMessage.objects.filter(conversation=conversation).order_by("sequence"))
        self.assertEqual([message.role for message in messages], ["user", "assistant"])
        self.assertEqual(str(messages[1].uid), events[0]["message_uid"])
        self.assertEqual(messages[1].rag_job.conversation_id, conversation.id)
        admission_token.release_async.assert_awaited_once()

    def test_job_and_message_link_roll_back_together(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        user_message, _ = append_user_message(
            conversation=conversation, user=self.owner, content="질문",
            client_request_id=uuid.uuid4(), node_ids=[],
        )
        with patch(
            "document_ai.search.execution.perform_vector_search_sync",
            return_value={"status": "success"},
        ), patch("document_ai.models.RAGMessage.objects.create", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                _create_rag_jobs_sync(
                    owner=self.owner, workspace=self.workspace, question="질문",
                    retrieval_query="질문", top_k=3, threshold=None, language="ko",
                    requested_node_ids=[], scoped_node_ids=[], llm_snapshot={},
                    conversation=conversation, reply_to=user_message,
                )
        self.assertFalse(RAGJob.objects.filter(conversation=conversation).exists())

    def test_search_exception_closes_reserved_conversation_job(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        user_message, _ = append_user_message(
            conversation=conversation, user=self.owner, content="질문",
            client_request_id=uuid.uuid4(), node_ids=[],
        )
        with patch(
            "document_ai.search.execution.perform_vector_search_sync",
            side_effect=RuntimeError("search crashed"),
        ):
            with self.assertRaisesMessage(RuntimeError, "search crashed"):
                _create_rag_jobs_sync(
                    owner=self.owner, workspace=self.workspace, question="질문",
                    retrieval_query="질문", top_k=3, threshold=None, language="ko",
                    requested_node_ids=[], scoped_node_ids=[], llm_snapshot={},
                    conversation=conversation, reply_to=user_message,
                )

        failed_job = RAGJob.objects.get(conversation=conversation)
        self.assertEqual(failed_job.status, AIStatus.FAILED)
        self.assertEqual(failed_job.error_message, "search crashed")
        self.assertIsNotNone(RAGMessage.objects.get(rag_job=failed_job))

    def test_expired_session_job_is_interrupted_and_no_longer_blocks_next_turn(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        expired_at = timezone.now() - timedelta(seconds=5)
        job = RAGJob.objects.create(
            workspace=self.workspace, owner=self.owner, conversation=conversation,
            question="멈춘 질문", status=AIStatus.PROCESSING,
            deadline_at=expired_at, lease_expires_at=expired_at,
        )
        recovered = recover_expired_rag_jobs(conversation=conversation)
        self.assertEqual(recovered, 1)
        job.refresh_from_db()
        self.assertEqual(job.status, "interrupted")
        self.assertIsNotNone(job.interrupted_at)

        self.assertTrue(
            RAGJob.objects.create(
                workspace=self.workspace, owner=self.owner, conversation=conversation,
                question="새 질문", status=AIStatus.PENDING,
                deadline_at=timezone.now() + timedelta(minutes=5),
                lease_expires_at=timezone.now() + timedelta(seconds=30),
            )
        )

    def test_message_reload_reconciles_an_expired_active_job(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner, title="복구 표시"
        )
        user_message, _ = append_user_message(
            conversation=conversation, user=self.owner, content="끊긴 질문",
            client_request_id=uuid.uuid4(), node_ids=[],
        )
        expired_at = timezone.now() - timedelta(seconds=5)
        job = RAGJob.objects.create(
            workspace=self.workspace, owner=self.owner, conversation=conversation,
            question="끊긴 질문", status=AIStatus.PROCESSING,
            deadline_at=expired_at, lease_expires_at=expired_at,
        )
        RAGMessage.objects.create(
            conversation=conversation, sequence=2, role=RAGMessage.ROLE_ASSISTANT,
            reply_to=user_message, rag_job=job, node_ids=[],
        )
        self._login(self.owner)

        response = self.client.get(
            f"/api/document-ai/v1/rag/conversations/{conversation.uid}/messages/"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"][1]["rag_job"]["status"], "interrupted")
        job.refresh_from_db()
        self.assertEqual(job.status, "interrupted")

    def test_lease_renewal_does_not_revive_expired_deadline(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        now = timezone.now()
        job = RAGJob.objects.create(
            workspace=self.workspace, owner=self.owner, conversation=conversation,
            question="만료된 질문", status=AIStatus.PROCESSING,
            deadline_at=now - timedelta(seconds=1),
            lease_expires_at=now + timedelta(seconds=20),
        )
        self.assertFalse(renew_rag_job_lease(rag_job_id=job.id, now=now))
        job.refresh_from_db()
        self.assertEqual(job.lease_heartbeat_at, None)

    def test_late_generation_completion_cannot_revive_interrupted_job(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        now = timezone.now()
        job = RAGJob.objects.create(
            workspace=self.workspace, owner=self.owner, conversation=conversation,
            question="중단된 질문", status="interrupted", interrupted_at=now,
            completed_at=now, error_message="RAG execution lease expired.",
        )
        context = AsyncGenerationContext(
            job_id=job.id, request_url="", payload={}, headers={}, request_timeout=30,
            citations=[], language="ko", metrics={}, worker_started=1.0,
            created_at=job.created_at, conversation_job=True,
        )

        result, final_delta = _complete_generation_sync(
            context, raw_answer="늦게 도착한 답변", emitted_answer=""
        )

        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(final_delta, "")
        job.refresh_from_db()
        self.assertEqual(job.status, "interrupted")
        self.assertEqual(job.answer, "")

    def test_late_cancel_cannot_overwrite_terminal_job(self):
        conversation = RAGConversation.objects.create(
            workspace=self.workspace, created_by=self.owner
        )
        job = RAGJob.objects.create(
            workspace=self.workspace, owner=self.owner, conversation=conversation,
            question="완료된 질문", status=AIStatus.COMPLETED, answer="완료 답변",
            completed_at=timezone.now(),
        )
        context = AsyncGenerationContext(
            job_id=job.id, request_url="", payload={}, headers={}, request_timeout=30,
            citations=[], language="ko", metrics={}, worker_started=1.0,
            created_at=job.created_at, conversation_job=True,
        )

        with patch("document_ai.services.rag_cancel_service.clear_rag_cancel_signal") as clear_signal:
            result = _mark_generation_canceled_sync(context)

        self.assertEqual(result, {"status": AIStatus.COMPLETED, "job_id": job.id, "updated": False})
        clear_signal.assert_called_once_with(job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, AIStatus.COMPLETED)
        self.assertEqual(job.answer, "완료 답변")
