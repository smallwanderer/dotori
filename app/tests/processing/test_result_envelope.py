from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from config.enums import AIStatus, NodeType
from document_ai.models import DocumentChunk, DocumentParseResult
from document_ai.parsers.schema import ChunkPayload, ParseResult
from document_ai.processing.parsing import save_parse_result
from document_ai.services.result_envelope import document_extract_result_envelope
from files.models import FileBlob, Node


pytestmark = pytest.mark.integration
User = get_user_model()


class DocumentResultEnvelopeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            email="result-envelope@example.com",
            password="password",
            is_active=True,
            email_verified=True,
        )
        self.node = Node.objects.create(
            owner=self.user,
            name="result.txt",
            ext=".txt",
            node_type=NodeType.FILE,
        )
        with patch("document_ai.signals.enqueue_parse"):
            self.blob = FileBlob.objects.create(
                node=self.node,
                original_name="result.txt",
                file=SimpleUploadedFile("result.txt", b"hello", content_type="text/plain"),
                mime_type="text/plain",
                size=5,
                sha256="source-v1",
                status="ready",
            )
        self.parse_result = DocumentParseResult.objects.create(
            node=self.node,
            parser_name="docling",
            parser_mode="convert",
            status=AIStatus.COMPLETED,
            input_document_hash="source-v1",
            chunk_count=1,
            metadata={
                "result_schema": "dotori.document.extract.v1",
                "source_blob_uuid": str(self.blob.uuid),
                "source_checksum": "source-v1",
                "processing_fingerprint": "parser-config-v1",
                "parser_version": "2.113.0",
            },
        )
        DocumentChunk.objects.create(
            parse_result=self.parse_result,
            chunk_index=0,
            text="hello",
            page_from=2,
            page_to=2,
            section_title="Intro",
        )

    def test_ready_result_exposes_source_processing_and_locator(self):
        envelope = document_extract_result_envelope(self.parse_result)

        self.assertEqual(envelope["status"], "ready")
        self.assertFalse(envelope["stale"])
        self.assertEqual(envelope["source"]["id"], str(self.node.uid))
        self.assertEqual(envelope["source"]["revision"], str(self.blob.uuid))
        self.assertEqual(envelope["processing"]["fingerprint"], "parser-config-v1")
        self.assertEqual(
            envelope["payload"]["chunks"][0]["locator"],
            {"type": "document", "page_from": 2, "page_to": 2, "section": "Intro"},
        )

    def test_source_checksum_change_marks_result_stale(self):
        FileBlob.objects.filter(pk=self.blob.pk).update(sha256="source-v2")
        self.parse_result.refresh_from_db()

        envelope = document_extract_result_envelope(self.parse_result)

        self.assertEqual(envelope["status"], "stale")
        self.assertTrue(envelope["stale"])
        self.assertEqual(envelope["source"]["checksum"], "source-v1")

    def test_parse_persistence_records_source_revision_and_processing_fingerprint(self):
        self.parse_result.delete()
        parsed = ParseResult(
            parser_mode="convert",
            parser_backend="docling",
            parser_version="2.113.0",
            file_path="result.txt",
            file_ext=".txt",
            input_document_hash="source-v1",
            status="success",
            chunks=[ChunkPayload(chunk_index=0, serialized_text="hello")],
        )

        with patch(
            "document_ai.processing.parsing._parse_metadata",
            return_value={
                "parser_backend": "docling",
                "parser_version": "2.113.0",
                "chunking_strategy": "hierarchical",
            },
        ):
            result = save_parse_result(self.node, parsed)

        self.assertEqual(result.metadata["result_schema"], "dotori.document.extract.v1")
        self.assertEqual(result.metadata["source_blob_uuid"], str(self.blob.uuid))
        self.assertEqual(result.metadata["source_checksum"], "source-v1")
        self.assertEqual(len(result.metadata["processing_fingerprint"]), 64)
