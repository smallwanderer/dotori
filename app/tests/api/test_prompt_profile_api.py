import json
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase

from document_ai.rag.generation import _build_generation_prompts
from document_ai.rag.prompt_profiles import get_effective_prompt_policy
from workspaces.models import WorkspaceMembership, WorkspaceQualityProfileRevision
from workspaces.services import create_team_workspace


User = get_user_model()

pytestmark = pytest.mark.unit


class PromptProfileApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="prompt-admin@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.member = User.objects.create_user(
            email="prompt-member@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.workspace = create_team_workspace(actor=self.admin, name="Prompt Team")
        WorkspaceMembership.objects.create(
            workspace=self.workspace,
            user=self.member,
            role=WorkspaceMembership.ROLE_MEMBER,
            status=WorkspaceMembership.STATUS_ACTIVE,
        )

    def request_json(self, method, path, payload=None):
        return getattr(self.client, method)(
            path,
            data=json.dumps(payload or {}),
            content_type="application/json",
        )

    def login_in_workspace(self, user):
        self.client.force_login(user)
        response = self.request_json(
            "post",
            "/api/workspaces/v1/switch/",
            {"workspace_uid": str(self.workspace.uid)},
        )
        self.assertEqual(response.status_code, 200)

    def save_draft(self, *, instruction="Use a concise table for comparisons."):
        return self.request_json(
            "patch",
            "/api/workspaces/v1/current/system-prompt/draft/",
            {
                "expected_revision": 0,
                "overrides": {
                    "document_rag": {
                        "mode": "replace",
                        "instruction": instruction,
                    }
                },
                "note": "workspace answer policy",
            },
        )

    def test_member_can_read_but_only_admin_can_edit(self):
        self.login_in_workspace(self.member)

        response = self.client.get("/api/workspaces/v1/current/system-prompt/")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["permissions"]["can_edit"])
        self.assertEqual(self.save_draft().status_code, 403)

    def test_draft_can_be_previewed_and_applied(self):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/system-prompt/")

        created = self.save_draft(instruction="Answer with a concise decision table.")

        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["draft"]["effective"]["document_rag"]["character_count"], 37)
        preview = self.request_json(
            "post",
            "/api/workspaces/v1/current/system-prompt/draft/preview/",
            {"expected_revision": 1, "route": "document_rag"},
        )
        self.assertEqual(preview.status_code, 200)
        self.assertIn("Answer with a concise decision table.", preview.json()["assembled_prompt"])
        self.assertIn("always take priority", preview.json()["assembled_prompt"])

        applied = self.request_json(
            "post",
            "/api/workspaces/v1/current/system-prompt/apply/",
            {
                "expected_revision": 1,
                "allow_unverified": True,
                "note": "manually reviewed prompt",
            },
        )
        self.assertEqual(applied.status_code, 200)
        self.assertEqual(
            get_effective_prompt_policy(self.workspace)["document_rag"]["instruction"],
            "Answer with a concise decision table.",
        )

    def test_applied_policy_is_used_by_generation_and_markup_is_escaped(self):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/system-prompt/")
        self.assertEqual(self.save_draft(instruction="Use <brief> answers.").status_code, 200)
        self.request_json(
            "post",
            "/api/workspaces/v1/current/system-prompt/apply/",
            {"expected_revision": 1, "allow_unverified": True, "note": "reviewed"},
        )
        job = SimpleNamespace(
            workspace=self.workspace,
            language="ko",
            question="정책은 무엇인가요?",
        )

        system_prompt, _, _, _ = _build_generation_prompts(
            job,
            skip_retrieval=False,
            context_text="[1] policy",
        )

        self.assertIn("Use &lt;brief&gt; answers.", system_prompt)
        self.assertIn("Only answer what is supported by the evidence", system_prompt)

    def test_prompt_apply_preserves_other_profile_axes(self):
        WorkspaceQualityProfileRevision.objects.create(
            workspace=self.workspace,
            version=1,
            revision=1,
            status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
            change_axis=WorkspaceQualityProfileRevision.AXIS_RETRIEVAL,
            retrieval_config={"search_top_k": 9},
            generation_config={"temperature": 0.4},
        )
        self.login_in_workspace(self.admin)
        self.assertEqual(self.save_draft().status_code, 200)

        self.request_json(
            "post",
            "/api/workspaces/v1/current/system-prompt/apply/",
            {"expected_revision": 1, "allow_unverified": True, "note": "reviewed"},
        )

        active = WorkspaceQualityProfileRevision.objects.get(
            workspace=self.workspace,
            status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
        )
        self.assertEqual(active.retrieval_config["search_top_k"], 9)
        self.assertEqual(active.generation_config["temperature"], 0.4)

    def test_invalid_or_oversized_instruction_is_rejected(self):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/system-prompt/")

        empty = self.save_draft(instruction="")
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()["error"]["code"], "PROFILE_VALIDATION_FAILED")
        oversized = self.save_draft(instruction="x" * 12_001)
        self.assertEqual(oversized.status_code, 400)
