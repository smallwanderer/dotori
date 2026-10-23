"""Workspace-scoped persistence and authorization for ordinary RAG conversations."""

from __future__ import annotations

import uuid
import os
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Max, Q
from django.utils import timezone

from config.enums import AIStatus, RAGStage
from document_ai.models import RAGConversation, RAGJob, RAGMessage
from document_ai.services.rag_cancel_service import set_rag_cancel_signal
from files.models import Node
from workspaces.models import WorkspaceMembership


RAG_INTERRUPTED = "interrupted"


class ConversationNotFound(Exception):
    pass


class ConversationPermissionDenied(Exception):
    pass


class ConversationRevisionConflict(Exception):
    pass


class ConversationIdempotencyConflict(Exception):
    pass


class InvalidConversationScope(Exception):
    def __init__(self, invalid_node_ids):
        self.invalid_node_ids = invalid_node_ids
        super().__init__("Conversation scope contains invalid or unavailable nodes.")


def _positive_int_setting(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(1, value)


def rag_execution_deadline_seconds() -> int:
    return _positive_int_setting("RAG_EXECUTION_DEADLINE_SECONDS", 360)


def rag_execution_lease_seconds() -> int:
    return min(
        _positive_int_setting("RAG_EXECUTION_LEASE_SECONDS", 30),
        rag_execution_deadline_seconds(),
    )


def recover_expired_rag_jobs(*, conversation=None, now=None) -> int:
    """Release active jobs whose process stopped renewing its lease."""
    now = now or timezone.now()
    active = RAGJob.objects.filter(status__in=[AIStatus.PENDING, AIStatus.PROCESSING])
    if conversation is not None:
        active = active.filter(conversation=conversation)
    expired = active.filter(Q(deadline_at__lte=now) | Q(lease_expires_at__lte=now))
    return expired.update(
        status=RAG_INTERRUPTED,
        stage=RAGStage.INTERRUPTED,
        stage_message="실행 프로세스가 중단되어 작업을 정리했습니다.",
        error_message="RAG execution lease expired.",
        completed_at=now,
        interrupted_at=now,
        updated_at=now,
    )


def renew_rag_job_lease(*, rag_job_id: int, now=None) -> bool:
    now = now or timezone.now()
    lease_expires_at = now + timedelta(seconds=rag_execution_lease_seconds())
    updated = RAGJob.objects.filter(
        pk=rag_job_id,
        status__in=[AIStatus.PENDING, AIStatus.PROCESSING],
        deadline_at__gt=now,
    ).update(
        lease_expires_at=lease_expires_at,
        lease_heartbeat_at=now,
        updated_at=now,
    )
    return updated == 1


def _is_workspace_admin(user, workspace) -> bool:
    return WorkspaceMembership.objects.filter(
        user=user, workspace=workspace, status=WorkspaceMembership.STATUS_ACTIVE,
        role=WorkspaceMembership.ROLE_ADMIN,
    ).exists()


def get_conversation(*, workspace, uid, include_deleted: bool = False) -> RAGConversation:
    queryset = RAGConversation.objects.filter(workspace=workspace, uid=uid)
    if not include_deleted:
        queryset = queryset.filter(deleted_at__isnull=True)
    conversation = queryset.first()
    if conversation is None:
        raise ConversationNotFound
    return conversation


def can_manage_conversation(*, user, conversation: RAGConversation) -> bool:
    return conversation.created_by_id == user.id or _is_workspace_admin(user, conversation.workspace)


def require_manager(*, user, conversation: RAGConversation) -> None:
    if not can_manage_conversation(user=user, conversation=conversation):
        raise ConversationPermissionDenied


def validate_conversation_node_ids(*, workspace, node_ids: list[str] | None) -> list[str]:
    normalized = []
    invalid = []
    for value in node_ids or []:
        try:
            normalized_value = str(uuid.UUID(str(value)))
        except (TypeError, ValueError, AttributeError):
            invalid.append(str(value))
            continue
        if normalized_value not in normalized:
            normalized.append(normalized_value)
    existing = {
        str(value)
        for value in Node.objects.filter(
            workspace=workspace, uid__in=normalized, trashed=False
        ).values_list("uid", flat=True)
    }
    invalid.extend(value for value in normalized if value not in existing)
    if invalid:
        raise InvalidConversationScope(invalid)
    return normalized


def create_conversation(*, workspace, user, title: str = "", default_node_ids: list[str] | None = None):
    validated_node_ids = validate_conversation_node_ids(
        workspace=workspace, node_ids=default_node_ids
    )
    return RAGConversation.objects.create(
        workspace=workspace, created_by=user, title=title.strip()[:160],
        default_node_ids=validated_node_ids,
    )


def update_conversation(*, conversation, user, title=None, default_node_ids=None, expected_revision: int):
    require_manager(user=user, conversation=conversation)
    updates = {"revision": F("revision") + 1, "updated_at": timezone.now()}
    if title is not None:
        updates["title"] = title.strip()[:160]
    if default_node_ids is not None:
        updates["default_node_ids"] = validate_conversation_node_ids(
            workspace=conversation.workspace, node_ids=default_node_ids
        )
    updated = RAGConversation.objects.filter(
        pk=conversation.pk, revision=expected_revision, deleted_at__isnull=True
    ).update(**updates)
    if updated != 1:
        raise ConversationRevisionConflict
    conversation.refresh_from_db()
    return conversation


def delete_conversation(*, conversation, user) -> RAGConversation:
    require_manager(user=user, conversation=conversation)
    with transaction.atomic():
        locked = RAGConversation.objects.select_for_update().get(pk=conversation.pk)
        recover_expired_rag_jobs(conversation=locked)
        if locked.rag_jobs.filter(status__in=[AIStatus.PENDING, AIStatus.PROCESSING]).exists():
            raise RuntimeError("CONVERSATION_BUSY")
        locked.deleted_at = timezone.now()
        locked.save(update_fields=["deleted_at", "updated_at"])
        conversation = locked
    return conversation


def request_conversation_cancel(*, conversation, user):
    """Signal the current generation to stop; repeated calls are safe."""
    require_manager(user=user, conversation=conversation)
    recover_expired_rag_jobs(conversation=conversation)
    active_job = conversation.rag_jobs.filter(
        status__in=[AIStatus.PENDING, AIStatus.PROCESSING]
    ).order_by("-created_at", "-id").first()
    if active_job is None:
        return None
    set_rag_cancel_signal(active_job.id)
    now = timezone.now()
    type(active_job).objects.filter(
        pk=active_job.pk,
        status__in=[AIStatus.PENDING, AIStatus.PROCESSING],
    ).update(
        cancel_requested_at=now,
        cancel_reason="conversation_cancel",
        updated_at=now,
    )
    active_job.cancel_requested_at = now
    active_job.cancel_reason = "conversation_cancel"
    return active_job


def append_user_message(*, conversation, user, content: str, client_request_id, node_ids: list[str]):
    """Persist an idempotent user turn; generation is attached separately."""
    require_manager(user=user, conversation=conversation)
    with transaction.atomic():
        locked = RAGConversation.objects.select_for_update().get(pk=conversation.pk)
        existing = RAGMessage.objects.filter(
            conversation=locked, role=RAGMessage.ROLE_USER, client_request_id=client_request_id,
        ).first()
        if existing is not None:
            if existing.content != content or existing.node_ids != node_ids:
                raise ConversationIdempotencyConflict
            return existing, False
        sequence = (RAGMessage.objects.filter(conversation=locked).aggregate(maximum=Max("sequence"))["maximum"] or 0) + 1
        message = RAGMessage.objects.create(
            conversation=locked, sequence=sequence, role=RAGMessage.ROLE_USER,
            created_by=user, content=content, client_request_id=client_request_id, node_ids=node_ids,
        )
        locked.save(update_fields=["updated_at"])
    return message, True


def reserve_session_turn(*, conversation, user, question: str, client_request_id, node_ids: list[str], retrieval_query: str, top_k: int, threshold: float | None, language: str, llm_snapshot: dict):
    """Atomically create the user/assistant/job records before admission."""
    from django.db.models import Max

    require_manager(user=user, conversation=conversation)
    with transaction.atomic():
        locked = RAGConversation.objects.select_for_update().get(pk=conversation.pk)
        recover_expired_rag_jobs(conversation=locked)
        existing = RAGMessage.objects.filter(
            conversation=locked, role=RAGMessage.ROLE_USER,
            client_request_id=client_request_id,
        ).first()
        if existing is not None:
            if existing.content != question or existing.node_ids != node_ids:
                raise ConversationIdempotencyConflict
            assistant = existing.replies.select_related("rag_job").first()
            return existing, assistant.rag_job if assistant is not None else None, False
        if locked.rag_jobs.filter(status__in=[AIStatus.PENDING, AIStatus.PROCESSING]).exists():
            raise RuntimeError("CONVERSATION_BUSY")
        now = timezone.now()
        sequence = (RAGMessage.objects.filter(conversation=locked).aggregate(maximum=Max("sequence"))["maximum"] or 0) + 1
        user_message = RAGMessage.objects.create(
            conversation=locked, sequence=sequence, role=RAGMessage.ROLE_USER,
            created_by=user, content=question, client_request_id=client_request_id,
            node_ids=node_ids,
        )
        rag_job = RAGJob.objects.create(
            owner=user, workspace=locked.workspace, conversation=locked,
            question=question, retrieval_query=retrieval_query,
            query_intent="document_question", answer_mode="rag", retrieval_required=True,
            top_k=top_k, language=language, node_ids=node_ids,
            stage=RAGStage.QUEUED, stage_message="답변 생성을 준비하고 있습니다.",
            deadline_at=now + timedelta(seconds=rag_execution_deadline_seconds()),
            lease_expires_at=now + timedelta(seconds=rag_execution_lease_seconds()),
            lease_heartbeat_at=now, **llm_snapshot,
        )
        assistant = RAGMessage.objects.create(
            conversation=locked, sequence=sequence + 1, role=RAGMessage.ROLE_ASSISTANT,
            reply_to=user_message, rag_job=rag_job, node_ids=node_ids,
        )
        locked.save(update_fields=["updated_at"])
    rag_job._conversation_message_uid = str(assistant.uid)
    rag_job._client_request_id = str(client_request_id)
    return user_message, rag_job, True


def fail_reserved_rag_job(*, rag_job_id: int, error: str):
    now = timezone.now()
    return RAGJob.objects.filter(
        pk=rag_job_id, status__in=[AIStatus.PENDING, AIStatus.PROCESSING]
    ).update(
        status=AIStatus.FAILED, stage=RAGStage.FAILED,
        stage_message="RAG 실행을 시작하지 못했습니다.", error_message=error,
        completed_at=now, updated_at=now,
    )


def get_existing_user_message(*, conversation, user, content: str, client_request_id, node_ids: list[str]):
    """Return a matching idempotent turn without creating another message."""
    require_manager(user=user, conversation=conversation)
    message = RAGMessage.objects.filter(
        conversation=conversation,
        role=RAGMessage.ROLE_USER,
        client_request_id=client_request_id,
    ).select_related("rag_job").first()
    if message is None:
        return None
    if message.content != content or message.node_ids != node_ids:
        raise ConversationIdempotencyConflict
    return message


def get_reply_job_for_message(message):
    """Load the assistant execution for a user message in sync DB context."""
    assistant = RAGMessage.objects.filter(reply_to=message).select_related("rag_job").first()
    return assistant.rag_job if assistant is not None else None
