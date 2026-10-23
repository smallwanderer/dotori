import json
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import JsonResponse
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts.cli_tokens import issue_cli_token
from config.enums import NodeType
from config.enums import AIStatus
from documents.models import DocumentChunk, DocumentParseResult
from documents.services.capability_admission import CapabilityAdmissionError
from files.models import FileBlob, Node
from workspaces.models import Workspace, WorkspaceMembership


pytestmark = pytest.mark.unit

@pytest.fixture(autouse=True)
def isolated_deployment(tmp_path, monkeypatch):
    """Do not inherit the operator's enabled capabilities or cached plan."""
    from capabilities.deployment import clear_deployment_snapshot_cache, load_deployment_snapshot
    from installation.deployment import build_module_deployment_plan, write_deployment_plan

    plan = build_module_deployment_plan(["documents"], disabled_provider_roles=("embedding", "llm"))
    path = write_deployment_plan(plan, repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    clear_deployment_snapshot_cache()
    snapshot = load_deployment_snapshot()
    assert snapshot.is_enabled("documents.extract")
    assert not snapshot.is_enabled("documents.semantic_search")
    yield
    clear_deployment_snapshot_cache()


User = get_user_model()


@override_settings(ALLOWED_HOSTS=["testserver", "localhost"], LOGIN_REQUIRED=True)
class CLIHttpApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="cli-api@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.other_user = User.objects.create_user(
            email="other-cli-api@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.token, self.secret = issue_cli_token(user=self.user, name="CLI API")
        self.client = Client(enforce_csrf_checks=True)

    @property
    def authorization(self):
        return f"Bearer {self.secret}"

    def _create_extraction_result(self, *, owner=None, workspace=None, trashed=False):
        owner = owner or self.user
        node = Node.objects.create(
            owner=owner,
            workspace=workspace,
            name="result.txt",
            ext=".txt",
            node_type=NodeType.FILE,
            trashed=trashed,
        )
        with patch("documents.signals.enqueue_parse"):
            blob = FileBlob.objects.create(
                node=node,
                original_name="result.txt",
                file=SimpleUploadedFile("result.txt", b"hello", content_type="text/plain"),
                mime_type="text/plain",
                size=5,
                sha256="source-v1",
                status="ready",
            )
        parse_result = DocumentParseResult.objects.create(
            node=node,
            parser_name="docling",
            parser_mode="convert",
            status=AIStatus.COMPLETED,
            input_document_hash="source-v1",
            chunk_count=1,
            metadata={
                "source_blob_uuid": str(blob.uuid),
                "source_checksum": "source-v1",
                "processing_fingerprint": "parser-config-v1",
            },
        )
        DocumentChunk.objects.create(
            parse_result=parse_result,
            chunk_index=0,
            text="hello",
        )
        return node, blob, parse_result

    def test_cli_routes_are_stable(self):
        self.assertEqual(reverse("cli_api:identity"), "/api/cli/v1/identity/")
        self.assertEqual(reverse("cli_api:status"), "/api/cli/v1/status/")
        self.assertEqual(reverse("cli_api:capabilities"), "/api/cli/v1/capabilities/")
        self.assertEqual(reverse("cli_api:documents"), "/api/cli/v1/documents/")
        self.assertEqual(reverse("cli_api:upload"), "/api/cli/v1/upload/")
        self.assertEqual(reverse("cli_api:search"), "/api/cli/v1/search/")
        self.assertEqual(reverse("cli_api:ask-stream"), "/api/cli/v1/ask/stream/")
        self.assertEqual(reverse("cli_api:results"), "/api/cli/v1/results/")

    def test_result_routes_are_stable(self):
        node, _, parse_result = self._create_extraction_result()
        self.assertEqual(
            reverse("cli_api:document-results", kwargs={"source_uid": node.uid}),
            f"/api/cli/v1/documents/{node.uid}/results/",
        )
        self.assertEqual(
            reverse(
                "cli_api:result-detail",
                kwargs={"result_id": f"document-extract:{parse_result.id}"},
            ),
            f"/api/cli/v1/results/document-extract:{parse_result.id}/",
        )

    def test_identity_returns_verified_account_token_and_reserved_workspace(self):
        response = self.client.get(
            reverse("cli_api:identity"),
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account"], {
            "id": str(self.user.pk),
            "email": self.user.email,
            "display_name": self.user.display_name,
        })
        self.assertEqual(response.json()["token"]["type"], "cli")
        self.assertEqual(response.json()["token"]["scopes"], self.token.scopes)
        self.assertIsNone(response.json()["workspace"])

    def test_identity_does_not_require_an_unrelated_capability_scope(self):
        self.token.scopes = []
        self.token.save(update_fields=["scopes"])

        response = self.client.get(
            reverse("cli_api:identity"),
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token"]["scopes"], [])

    def test_document_list_uses_token_owner_and_needs_no_session_csrf(self):
        own = Node.objects.create(
            owner=self.user,
            name="own.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )
        Node.objects.create(
            owner=self.other_user,
            name="private.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )

        response = self.client.get(
            reverse("cli_api:documents"),
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["uid"] for item in response.json()["files"]],
            [str(own.uid)],
        )

    @patch("documents.signals.enqueue_parse")
    def test_upload_uses_cli_token_owner_without_csrf(self, parse_delay):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("cli_api:upload"),
                data={
                    "file": SimpleUploadedFile(
                        "cli.txt",
                        b"from cli",
                        content_type="text/plain",
                    )
                },
                HTTP_AUTHORIZATION=self.authorization,
            )
            parse_delay.assert_not_called()

        self.assertEqual(response.status_code, 200)
        node = Node.objects.get(uid=response.json()["file"]["uid"])
        self.assertEqual(node.owner, self.user)
        parse_delay.assert_called_once()

    def test_read_only_token_cannot_upload(self):
        _, read_only_secret = issue_cli_token(
            user=self.user,
            name="MCP read only",
            access_level="read_only",
        )

        response = self.client.post(
            reverse("cli_api:upload"),
            data={
                "file": SimpleUploadedFile(
                    "denied.txt",
                    b"must not upload",
                    content_type="text/plain",
                )
            },
            HTTP_AUTHORIZATION=f"Bearer {read_only_secret}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "CLI_TOKEN_SCOPE_REQUIRED")
        self.assertFalse(Node.objects.filter(owner=self.user, name="denied.txt").exists())

    def test_search_uses_cli_token_owner(self):
        with patch(
            "documents.search.views.search_documents_sync",
            return_value=([], {"request_search_ms": 1.5}),
        ) as search:
            response = self.client.post(
                reverse("cli_api:search"),
                data=json.dumps({"mode": "basic", "query": "policy"}),
                content_type="application/json",
                HTTP_AUTHORIZATION=self.authorization,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(search.call_args.kwargs["owner"], self.user)

    def test_search_reports_disabled_semantic_search_capability(self):
        error = CapabilityAdmissionError(
            "documents.semantic_search",
            "CAPABILITY_DISABLED",
            "Capability documents.semantic_search is disabled on this server",
        )
        with patch(
            "documents.search.views.search_documents_sync",
            side_effect=error,
        ):
            response = self.client.post(
                reverse("cli_api:search"),
                data=json.dumps({"mode": "basic", "query": "policy"}),
                content_type="application/json",
                HTTP_AUTHORIZATION=self.authorization,
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "CAPABILITY_DISABLED")
        self.assertFalse(response.json()["error"]["details"]["retryable"])

    def test_status_uses_cli_token_owner(self):
        def status_response(request):
            return JsonResponse({"ok": True, "user": request.user.email})

        with patch("documents.cli_views.server_policy", side_effect=status_response):
            response = self.client.get(
                reverse("cli_api:status"),
                HTTP_AUTHORIZATION=self.authorization,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"], self.user.email)

    @patch("documents.cli_views.build_server_policy_payload")
    def test_capabilities_reuses_server_policy_contract(self, build_policy):
        build_policy.return_value = {"capabilities": {"documents.extract": {"enabled": True}}}
        response = self.client.get(
            reverse("cli_api:capabilities"),
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["capabilities"]["documents.extract"]["enabled"])

    def test_result_list_and_detail_use_common_envelope(self):
        node, _, parse_result = self._create_extraction_result()
        listed = self.client.get(
            reverse("cli_api:document-results", kwargs={"source_uid": node.uid}),
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(listed.status_code, 200)
        summary = listed.json()["results"][0]
        self.assertEqual(summary["id"], f"document-extract:{parse_result.id}")
        self.assertEqual(summary["kind"], "documents.extract")
        self.assertEqual(summary["capability_id"], "documents.extract")
        self.assertNotIn("payload", summary)

        detail = self.client.get(
            reverse("cli_api:result-detail", kwargs={"result_id": summary["id"]}),
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["result"]["payload"]["chunks"][0]["text"], "hello")

        all_results = self.client.get(
            reverse("cli_api:results"),
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(all_results.status_code, 200)
        self.assertEqual(all_results.json()["results"][0]["id"], summary["id"])

    def test_result_list_rejects_invalid_source_uid(self):
        response = self.client.get(
            reverse("cli_api:results"),
            {"source_uid": "not-a-uuid"},
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_SOURCE_UID")

    def test_result_access_hides_other_workspace_and_trashed_sources(self):
        other_node, _, other_result = self._create_extraction_result(owner=self.other_user)
        own_node, _, _ = self._create_extraction_result(trashed=True)

        other = self.client.get(
            reverse(
                "cli_api:result-detail",
                kwargs={"result_id": f"document-extract:{other_result.id}"},
            ),
            HTTP_AUTHORIZATION=self.authorization,
        )
        trashed = self.client.get(
            reverse("cli_api:document-results", kwargs={"source_uid": own_node.uid}),
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(other.status_code, 404)
        self.assertEqual(trashed.status_code, 404)

    def test_result_access_reflects_workspace_membership_revocation(self):
        team = Workspace.objects.create(
            name="Result Team",
            kind=Workspace.KIND_TEAM,
            created_by=self.user,
        )
        membership = WorkspaceMembership.objects.create(
            workspace=team,
            user=self.user,
            role=WorkspaceMembership.ROLE_ADMIN,
            status=WorkspaceMembership.STATUS_ACTIVE,
        )
        node, _, _ = self._create_extraction_result(workspace=team)
        url = reverse("cli_api:document-results", kwargs={"source_uid": node.uid})
        allowed = self.client.get(
            url,
            HTTP_AUTHORIZATION=self.authorization,
            HTTP_X_DOTORI_WORKSPACE=str(team.uid),
        )
        membership.status = WorkspaceMembership.STATUS_PENDING
        membership.save(update_fields=["status"])
        denied = self.client.get(
            url,
            HTTP_AUTHORIZATION=self.authorization,
            HTTP_X_DOTORI_WORKSPACE=str(team.uid),
        )
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(denied.status_code, 403)

    def test_result_stale_state_is_computed_when_read(self):
        _, blob, parse_result = self._create_extraction_result()
        FileBlob.objects.filter(pk=blob.pk).update(sha256="source-v2")
        response = self.client.get(
            reverse(
                "cli_api:result-detail",
                kwargs={"result_id": f"document-extract:{parse_result.id}"},
            ),
            HTTP_AUTHORIZATION=self.authorization,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["result"]["stale"])

    def test_ask_authenticates_before_rag_validation(self):
        response = self.client.post(
            reverse("cli_api:ask-stream"),
            data=json.dumps({}),
            content_type="application/json",
            HTTP_AUTHORIZATION=self.authorization,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")

    def test_missing_scope_and_legacy_sync_token_are_rejected(self):
        self.token.scopes = ["status:read"]
        self.token.save(update_fields=["scopes"])
        denied = self.client.get(
            reverse("cli_api:documents"),
            HTTP_AUTHORIZATION=self.authorization,
        )
        missing = self.client.get(reverse("cli_api:documents"))

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["error"]["code"], "CLI_TOKEN_SCOPE_REQUIRED")
        self.assertEqual(missing.status_code, 401)
