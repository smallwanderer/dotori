import json

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase

from document_ai.search.profiles import get_effective_generation_config
from workspaces.models import WorkspaceMembership, WorkspaceQualityProfileRevision
from workspaces.services import create_team_workspace


User = get_user_model()

pytestmark = pytest.mark.unit


class GenerationProfileApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="generation-admin@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.member = User.objects.create_user(
            email="generation-member@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.workspace = create_team_workspace(actor=self.admin, name="Generation Team")
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

    def save_draft(self, **overrides):
        payload = {
            "expected_revision": 0,
            "overrides": {
                "max_output_tokens": 768,
                "temperature": 0.4,
                "top_p": 0.85,
                **overrides,
            },
            "reset_fields": [],
            "note": "generation candidate",
        }
        return self.request_json(
            "patch",
            "/api/workspaces/v1/current/generation-profile/draft/",
            payload,
        )

    def test_get_creates_workspace_scoped_active_profile(self):
        self.login_in_workspace(self.admin)

        response = self.client.get("/api/workspaces/v1/current/generation-profile/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["workspace_uid"], str(self.workspace.uid))
        self.assertEqual(body["axis"], "generation")
        self.assertIsNone(body["draft"])
        self.assertIn("temperature", body["schema"])
        self.assertEqual(
            WorkspaceQualityProfileRevision.objects.filter(
                workspace=self.workspace,
                status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
            ).count(),
            1,
        )

    def test_member_can_read_but_cannot_change_profile(self):
        self.login_in_workspace(self.member)

        response = self.client.get("/api/workspaces/v1/current/generation-profile/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["permissions"]["can_edit"])

        response = self.save_draft()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_draft_uses_optimistic_revision_and_can_be_discarded(self):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/generation-profile/")

        created = self.save_draft(max_output_tokens=900)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["draft"]["revision"], 1)
        self.assertEqual(created.json()["draft"]["effective"]["max_output_tokens"], 900)

        stale = self.save_draft(max_output_tokens=1000)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "PROFILE_REVISION_CONFLICT")

        discarded = self.request_json(
            "post",
            "/api/workspaces/v1/current/generation-profile/draft/discard/",
            {"expected_revision": 1},
        )
        self.assertEqual(discarded.status_code, 200)
        self.assertIsNone(discarded.json()["draft"])

    def test_unverified_apply_requires_explicit_flag_and_note(self):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/generation-profile/")
        self.assertEqual(self.save_draft(temperature=0.7).status_code, 200)

        blocked = self.request_json(
            "post",
            "/api/workspaces/v1/current/generation-profile/apply/",
            {"expected_revision": 1, "allow_unverified": False, "note": "candidate"},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "EVALUATION_REQUIRED")

        applied = self.request_json(
            "post",
            "/api/workspaces/v1/current/generation-profile/apply/",
            {"expected_revision": 1, "allow_unverified": True, "note": "manual verification"},
        )
        self.assertEqual(applied.status_code, 200)
        body = applied.json()
        self.assertEqual(body["active"]["version"], 2)
        self.assertEqual(body["active"]["effective"]["temperature"], 0.7)
        self.assertEqual(body["active"]["validation"]["state"], "unverified")
        self.assertIsNone(body["draft"])

    def test_applied_profile_is_read_by_get_effective_generation_config(self):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/generation-profile/")
        self.assertEqual(
            self.save_draft(max_output_tokens=640, temperature=1.1, top_p=0.5).status_code,
            200,
        )
        self.request_json(
            "post",
            "/api/workspaces/v1/current/generation-profile/apply/",
            {"expected_revision": 1, "allow_unverified": True, "note": "manual verification"},
        )

        effective = get_effective_generation_config(self.workspace)

        self.assertEqual(effective["max_output_tokens"], 640)
        self.assertEqual(effective["temperature"], 1.1)
        self.assertEqual(effective["top_p"], 0.5)

    def test_out_of_range_temperature_is_rejected(self):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/generation-profile/")

        response = self.request_json(
            "patch",
            "/api/workspaces/v1/current/generation-profile/draft/",
            {
                "expected_revision": 0,
                "overrides": {"max_output_tokens": 512, "temperature": 3.5, "top_p": 0.9},
                "reset_fields": [],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "PROFILE_VALIDATION_FAILED")

    def test_draft_axis_conflict_between_retrieval_and_generation(self):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/retrieval-profile/")
        retrieval_draft = self.request_json(
            "patch",
            "/api/workspaces/v1/current/retrieval-profile/draft/",
            {
                "expected_revision": 0,
                "overrides": {"dense_weight": 0.4, "sparse_weight": 0.6},
                "reset_fields": [],
                "note": "retrieval candidate",
            },
        )
        self.assertEqual(retrieval_draft.status_code, 200)

        blocked = self.save_draft()
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "DRAFT_AXIS_CONFLICT")
