import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class Workspace(models.Model):
    KIND_PERSONAL = "personal"
    KIND_TEAM = "team"
    KIND_CHOICES = [
        (KIND_PERSONAL, "Personal"),
        (KIND_TEAM, "Team"),
    ]

    uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    name = models.CharField(max_length=128)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_TEAM, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_workspaces",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name


class WorkspaceMembership(models.Model):
    ROLE_ADMIN = "admin"
    ROLE_MEMBER = "member"
    ROLE_CHOICES = [
        (ROLE_ADMIN, "Admin"),
        (ROLE_MEMBER, "Member"),
    ]

    STATUS_ACTIVE = "active"
    STATUS_PENDING = "pending"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_PENDING, "Pending"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_memberships",
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES, default=ROLE_MEMBER)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_workspace_invites",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["workspace", "user"],
                name="uniq_membership_per_workspace_user",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["workspace", "status"]),
        ]

    def __str__(self):
        return f"{self.user} @ {self.workspace} ({self.role}/{self.status})"

    @property
    def is_admin(self) -> bool:
        return self.role == self.ROLE_ADMIN


def generate_invite_token() -> str:
    return secrets.token_urlsafe(24)


def invite_token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_invite_code() -> str:
    """Historical migration compatibility; new codes use issue()."""
    return secrets.token_hex(8).upper()


class WorkspaceInviteCode(models.Model):
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invite_codes")
    code_digest = models.CharField(max_length=64, unique=True, db_index=True)
    code_prefix = models.CharField(max_length=12, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_invite_codes",
    )
    is_active = models.BooleanField(default=True)
    max_uses = models.PositiveIntegerField(null=True, blank=True)
    use_count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_redeemable(self) -> bool:
        if not self.is_active:
            return False
        if self.expires_at and self.expires_at <= timezone.now():
            return False
        if self.max_uses is not None and self.use_count >= self.max_uses:
            return False
        return True

    def __str__(self):
        return f"{self.code_prefix}... ({self.workspace})"

    @classmethod
    def issue(cls, *, workspace, created_by, max_uses=None, expires_at=None):
        token = generate_invite_token()
        invite = cls.objects.create(
            workspace=workspace,
            code_digest=invite_token_digest(token),
            code_prefix=token[:8],
            created_by=created_by,
            max_uses=max_uses,
            expires_at=expires_at,
        )
        return invite, token


class WorkspaceInvitation(models.Model):
    STATUS_PENDING = "pending"
    STATUS_ACCEPTED = "accepted"
    STATUS_DECLINED = "declined"
    STATUS_REVOKED = "revoked"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_ACCEPTED, "Accepted"),
        (STATUS_DECLINED, "Declined"),
        (STATUS_REVOKED, "Revoked"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="invitations")
    invitee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_invitations",
    )
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="issued_workspace_invitations",
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["invitee", "status", "-created_at"], name="workspace_i_invitee_status_idx"),
            models.Index(fields=["workspace", "status", "-created_at"], name="workspace_i_ws_status_idx"),
        ]


# WorkspaceQualityProfileRevision (the workspace's applied/draft search
# quality policy) and WorkspaceEvaluationDataset/WorkspaceQualityEvaluationRun
# (its evaluation inputs/outputs) used to live here. All three are B4
# (documents) domain state scoped by workspace -- comparable to
# documents.models.rag.UserLLMPreference, which already stores a per-
# workspace RAG setting in `documents_*` the same way. They moved to
# documents.models.evaluation; see dev-docs/block-ownership-violations-plan.md
# item 3.
