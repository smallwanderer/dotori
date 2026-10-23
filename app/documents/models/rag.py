import uuid

from django.db import models
from django.conf import settings

from config.enums import AIStatus, QueryAnswerMode, QueryIntent, RAGStage

from ._common import _ensure_workspace_from_owner


RAG_STATUS_CHOICES = (*AIStatus.choices, ("interrupted", "Interrupted"))


class LLMEndpoint(models.Model):
    """OpenAI-compatible LLM endpoint registered by a user."""

    ENDPOINT_OPENAI_COMPATIBLE = "openai_compatible"
    ENDPOINT_OLLAMA = "ollama"
    ENDPOINT_LLAMA_CPP = "llama_cpp"
    ENDPOINT_VLLM = "vllm"

    ENDPOINT_TYPE_CHOICES = [
        (ENDPOINT_OPENAI_COMPATIBLE, "OpenAI compatible"),
        (ENDPOINT_OLLAMA, "Ollama"),
        (ENDPOINT_LLAMA_CPP, "llama.cpp"),
        (ENDPOINT_VLLM, "vLLM"),
    ]

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="documents_llm_endpoints",
    )
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="llm_endpoints",
    )
    name = models.CharField(max_length=128)
    endpoint_type = models.CharField(
        max_length=32,
        choices=ENDPOINT_TYPE_CHOICES,
        default=ENDPOINT_OPENAI_COMPATIBLE,
    )
    base_url = models.URLField(
        max_length=512,
        help_text="Base URL without /v1 suffix when the endpoint already exposes OpenAI-compatible routes.",
    )
    default_model = models.CharField(max_length=256)
    api_key = models.CharField(max_length=512, blank=True)
    is_active = models.BooleanField(default=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_check_status = models.CharField(max_length=32, blank=True)
    last_check_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "name"],
                name="uniq_llm_endpoint_name_per_workspace",
            )
        ]
        indexes = [
            models.Index(fields=["owner", "is_active"], name="docs_llmep_owner_active_idx"),
            models.Index(fields=["workspace", "is_active"], name="llmendpoint_ws_active_idx"),
        ]

    def save(self, *args, **kwargs):
        _ensure_workspace_from_owner(self)
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.owner or 'deleted user'})"

    @property
    def normalized_base_url(self) -> str:
        base_url = self.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3].rstrip("/")
        return base_url

    @property
    def chat_completions_url(self) -> str:
        return f"{self.normalized_base_url}/v1/chat/completions"

    @property
    def responses_url(self) -> str:
        return f"{self.normalized_base_url}/v1/responses"


class UserLLMPreference(models.Model):
    """Workspace-wide defaults for LLM-backed document AI tasks.

    `user` is kept only as a legacy audit reference to whoever originally
    configured the preference; `workspace` is the sole scope going forward
    so team members share one RAG default.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="legacy_llm_preference",
    )
    workspace = models.OneToOneField(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="llm_preference",
    )
    rag_endpoint = models.ForeignKey(
        "documents.LLMEndpoint",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_preferences",
    )
    rag_model = models.CharField(max_length=256, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        if not self.workspace_id and self.user_id:
            from workspaces.models import WorkspaceMembership

            self.workspace_id = WorkspaceMembership.objects.filter(
                user_id=self.user_id,
                status=WorkspaceMembership.STATUS_ACTIVE,
                workspace__kind="personal",
            ).values_list("workspace_id", flat=True).first()
        if not self.workspace_id:
            raise ValueError("A workspace is required.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"LLM preferences for {self.workspace}"

    def get_rag_model(self) -> str:
        if self.rag_model:
            return self.rag_model
        if self.rag_endpoint_id and self.rag_endpoint:
            return self.rag_endpoint.default_model
        return ""


class RAGJob(models.Model):
    workspace = models.ForeignKey(
        "workspaces.Workspace",
        on_delete=models.CASCADE,
        related_name="rag_jobs",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="documents_rag_jobs",
    )
    conversation = models.ForeignKey(
        "documents.RAGConversation",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="rag_jobs",
    )
    search_job = models.ForeignKey(
        "documents.SearchJob",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_jobs",
    )
    query_log = models.ForeignKey(
        "documents.QueryUnderstandingLog",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_jobs",
    )

    question = models.TextField()
    retrieval_query = models.TextField(blank=True)
    query_intent = models.CharField(
        max_length=64,
        choices=QueryIntent.choices,
        default=QueryIntent.AMBIGUOUS,
        db_index=True,
    )
    answer_mode = models.CharField(
        max_length=64,
        choices=QueryAnswerMode.choices,
        default=QueryAnswerMode.RAG,
    )
    retrieval_required = models.BooleanField(default=True)
    query_confidence = models.FloatField(default=0.0)
    top_k = models.PositiveIntegerField(default=5)
    language = models.CharField(max_length=8, default="ko")
    node_ids = models.JSONField(default=list, blank=True)
    llm_endpoint = models.ForeignKey(
        "documents.LLMEndpoint",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_jobs",
    )
    llm_endpoint_name = models.CharField(max_length=128, blank=True)
    llm_base_url = models.URLField(max_length=512, blank=True)
    llm_model = models.CharField(max_length=256, blank=True)

    status = models.CharField(
        max_length=32,
        choices=RAG_STATUS_CHOICES,
        default=AIStatus.PENDING,
        db_index=True,
    )
    stage = models.CharField(
        max_length=32,
        choices=RAGStage.choices,
        default=RAGStage.QUEUED,
        db_index=True,
    )
    stage_message = models.CharField(max_length=255, blank=True)
    task_id = models.CharField(max_length=255, blank=True)
    answer = models.TextField(blank=True)
    citations = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    canceled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    deadline_at = models.DateTimeField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    lease_heartbeat_at = models.DateTimeField(null=True, blank=True)
    interrupted_at = models.DateTimeField(null=True, blank=True)
    performance_metrics = models.JSONField(default=dict, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["workspace", "status", "-created_at"], name="docai_ra_ws_status_created_idx"),
            models.Index(fields=["owner", "status", "-created_at"], name="docs_rag_own_st_created_idx"),
            models.Index(fields=["workspace", "owner", "status", "-completed_at"], name="docai_rag_ws_own_st_cmp_idx"),
            models.Index(fields=["workspace", "owner", "-created_at"], name="docai_rag_ws_own_created_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation"],
                condition=models.Q(
                    conversation__isnull=False,
                    status__in=[AIStatus.PENDING, AIStatus.PROCESSING],
                ),
                name="docai_rag_one_active_conversation",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"RAGJob({self.id}) {self.status}: {self.question[:40]}"

    def save(self, *args, **kwargs):
        _ensure_workspace_from_owner(self)
        if self.conversation_id and self.conversation.workspace_id != self.workspace_id:
            raise ValueError("RAG job and conversation must belong to the same workspace.")
        if self.search_job_id and self.search_job.workspace_id != self.workspace_id:
            raise ValueError("RAG and search jobs must belong to the same workspace.")
        if self.query_log_id and self.query_log.workspace_id != self.workspace_id:
            raise ValueError("RAG job and query log must belong to the same workspace.")
        return super().save(*args, **kwargs)


class RAGConversation(models.Model):
    """A workspace-scoped container for ordinary RAG turns.

    The workspace is the data-isolation boundary. ``created_by`` records who
    may continue or manage the conversation; it is deliberately nullable so
    account removal does not remove workspace history.
    """

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    workspace = models.ForeignKey(
        "workspaces.Workspace", on_delete=models.CASCADE, related_name="rag_conversations"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_conversations",
    )
    title = models.CharField(max_length=160, blank=True)
    default_node_ids = models.JSONField(default=list, blank=True)
    revision = models.PositiveIntegerField(default=1)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at", "-id"]
        indexes = [
            models.Index(
                fields=["workspace", "-updated_at", "-id"],
                name="docai_conv_ws_updated_idx",
            ),
        ]

    def __str__(self):
        return f"RAGConversation({self.uid})"


class RAGMessage(models.Model):
    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_CHOICES = [(ROLE_USER, "User"), (ROLE_ASSISTANT, "Assistant")]

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    conversation = models.ForeignKey(
        RAGConversation, on_delete=models.CASCADE, related_name="messages"
    )
    sequence = models.PositiveIntegerField()
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rag_messages",
    )
    content = models.TextField(blank=True)
    client_request_id = models.UUIDField(null=True, blank=True)
    node_ids = models.JSONField(default=list, blank=True)
    reply_to = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="replies"
    )
    rag_job = models.OneToOneField(
        RAGJob,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="conversation_message",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sequence", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "sequence"], name="docai_msg_conversation_sequence_uniq"
            ),
            models.UniqueConstraint(
                fields=["conversation", "client_request_id"],
                condition=models.Q(role="user", client_request_id__isnull=False),
                name="docai_user_message_request_uniq",
            ),
        ]

    def __str__(self):
        return f"RAGMessage({self.conversation_id}:{self.sequence} {self.role})"

    def save(self, *args, **kwargs):
        if self.reply_to_id and self.reply_to.conversation_id != self.conversation_id:
            raise ValueError("Reply and message must belong to the same conversation.")
        if self.rag_job_id:
            if self.rag_job.conversation_id != self.conversation_id:
                raise ValueError("RAG job and message must belong to the same conversation.")
            if self.rag_job.workspace_id != self.conversation.workspace_id:
                raise ValueError("RAG job and message must belong to the same workspace.")
        return super().save(*args, **kwargs)
