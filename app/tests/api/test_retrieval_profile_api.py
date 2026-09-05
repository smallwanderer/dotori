import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase

from workspaces.models import WorkspaceMembership, WorkspaceQualityProfileRevision
from workspaces.services import create_team_workspace


User = get_user_model()

pytestmark = pytest.mark.unit


class RetrievalProfileApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="quality-admin@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.member = User.objects.create_user(
            email="quality-member@example.com",
            password="test-pass",
            email_verified=True,
            is_active=True,
        )
        self.workspace = create_team_workspace(actor=self.admin, name="Quality Team")
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
                "dense_weight": 0.4,
                "sparse_weight": 0.6,
                **overrides,
            },
            "reset_fields": [],
            "note": "retrieval candidate",
        }
        return self.request_json(
            "patch",
            "/api/workspaces/v1/current/retrieval-profile/draft/",
            payload,
        )

    def test_get_creates_workspace_scoped_active_profile(self):
        self.login_in_workspace(self.admin)

        response = self.client.get("/api/workspaces/v1/current/retrieval-profile/")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["workspace_uid"], str(self.workspace.uid))
        self.assertEqual(body["axis"], "retrieval")
        self.assertEqual(body["active"]["version"], 1)
        self.assertIsNone(body["draft"])
        self.assertIn("candidate_multiplier", body["schema"])
        self.assertEqual(
            WorkspaceQualityProfileRevision.objects.filter(
                workspace=self.workspace,
                status=WorkspaceQualityProfileRevision.STATUS_ACTIVE,
            ).count(),
            1,
        )

    def test_member_can_read_but_cannot_change_profile(self):
        self.login_in_workspace(self.member)

        response = self.client.get("/api/workspaces/v1/current/retrieval-profile/")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["permissions"]["can_edit"])

        response = self.save_draft()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "PERMISSION_DENIED")

    def test_draft_uses_optimistic_revision_and_can_be_discarded(self):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/retrieval-profile/")

        created = self.save_draft(search_top_k=9)
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.json()["draft"]["revision"], 1)
        self.assertEqual(created.json()["draft"]["effective"]["search_top_k"], 9)

        stale = self.save_draft(search_top_k=10)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "PROFILE_REVISION_CONFLICT")

        discarded = self.request_json(
            "post",
            "/api/workspaces/v1/current/retrieval-profile/draft/discard/",
            {"expected_revision": 1},
        )
        self.assertEqual(discarded.status_code, 200)
        self.assertIsNone(discarded.json()["draft"])

    def test_unverified_apply_requires_explicit_flag_and_note(self):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/retrieval-profile/")
        self.assertEqual(self.save_draft(search_top_k=9).status_code, 200)

        blocked = self.request_json(
            "post",
            "/api/workspaces/v1/current/retrieval-profile/apply/",
            {"expected_revision": 1, "allow_unverified": False, "note": "candidate"},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["error"]["code"], "EVALUATION_REQUIRED")

        applied = self.request_json(
            "post",
            "/api/workspaces/v1/current/retrieval-profile/apply/",
            {"expected_revision": 1, "allow_unverified": True, "note": "manual verification"},
        )
        self.assertEqual(applied.status_code, 200)
        body = applied.json()
        self.assertEqual(body["active"]["version"], 2)
        self.assertEqual(body["active"]["effective"]["search_top_k"], 9)
        self.assertEqual(body["active"]["validation"]["state"], "unverified")
        self.assertIsNone(body["draft"])

    @patch("document_ai.search.views.profile_threshold_to_retriever", return_value=0.123)
    @patch("document_ai.search.views.search_documents_sync", return_value=([], {"result_count": 0}))
    def test_active_profile_is_used_by_normal_search(self, search_documents, _threshold):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/retrieval-profile/")
        self.assertEqual(self.save_draft(search_top_k=9).status_code, 200)
        self.request_json(
            "post",
            "/api/workspaces/v1/current/retrieval-profile/apply/",
            {"expected_revision": 1, "allow_unverified": True, "note": "manual verification"},
        )

        response = self.request_json(
            "post",
            "/api/document-ai/v1/search/",
            {"query": "workspace profile"},
        )

        self.assertEqual(response.status_code, 200)
        kwargs = search_documents.call_args.kwargs
        self.assertEqual(kwargs["top_k"], 9)
        self.assertEqual(kwargs["threshold"], 0.123)
        self.assertEqual(kwargs["tuning_params"]["dense_weight"], 0.4)
        self.assertEqual(kwargs["tuning_params"]["sparse_weight"], 0.6)

    def test_invalid_weight_pair_is_rejected(self):
        self.login_in_workspace(self.admin)
        self.client.get("/api/workspaces/v1/current/retrieval-profile/")

        response = self.request_json(
            "patch",
            "/api/workspaces/v1/current/retrieval-profile/draft/",
            {
                "expected_revision": 0,
                "overrides": {"dense_weight": 0.8, "sparse_weight": 0.8},
                "reset_fields": [],
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "PROFILE_VALIDATION_FAILED")
