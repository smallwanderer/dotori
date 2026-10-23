"""Request-level recovery never grants takeover or legacy scope expansion."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from documents.models import ExecutorJobReceipt
from documents.processing.recovery import scoped_recovery_blocked_node_ids, get_parse_recovery_node_ids
from documents.services.executor_jobs import claim_execution, fail_execution
from tests.processing.test_embedding_preparation import deployment, doc, parsed
from tests.processing.test_embedding_manifests import approval
from tests.embedding.test_scoped_embedding_execution import scoped
from tests.embedding.test_embedding_persistence import isolated_runtime_cache

pytestmark = pytest.mark.django_db


def claim(envelope, manifest):
    return claim_execution(job_id=envelope["job_id"], work_kind="embed_document",
        target_key="manifest:" + str(manifest.pk), payload=envelope["payload"],
        source_snapshot={"node_ids": [manifest.node_id]}, runtime_snapshot={"fingerprint": "runtime"})


def test_old_running_request_is_not_taken_over_and_blocks_legacy_parse(scoped, doc):
    envelope, manifest = scoped.configure()
    receipt = claim(envelope, manifest).receipt
    ExecutorJobReceipt.objects.filter(pk=receipt.pk).update(updated_at=timezone.now() - timedelta(days=1))
    replay = claim(envelope, manifest)
    assert not replay.should_execute
    assert replay.receipt.status == "running" and replay.receipt.attempt == 1
    assert list(scoped_recovery_blocked_node_ids()) == [doc.node.pk]
    # Even a newly missing parse result cannot silently replace approved input.
    doc.node.parse_result.delete()
    assert doc.node.pk not in get_parse_recovery_node_ids(50)


def test_exhausted_request_preserves_job_and_blocks_legacy_recovery(scoped, doc):
    envelope, manifest = scoped.configure()
    for attempt in range(1, 5):
        receipt = claim(envelope, manifest).receipt
        assert receipt.attempt == attempt
        fail_execution(receipt, code="TEMPORARY", message="test retry", retryable=True)
    exhausted = claim(envelope, manifest)
    assert not exhausted.should_execute
    assert exhausted.receipt.status == "failed" and exhausted.receipt.attempt == 4
    assert exhausted.receipt.error["code"] == "RETRIES_EXHAUSTED"
    assert exhausted.receipt.retryable is False
    assert list(scoped_recovery_blocked_node_ids()) == [doc.node.pk]
    assert claim(envelope, manifest).receipt.attempt == 4


@pytest.mark.parametrize("running", [True, False])
def test_source_change_releases_failed_legacy_block_but_not_running(scoped, doc, running):
    envelope, manifest = scoped.configure()
    receipt = claim(envelope, manifest).receipt
    if not running:
        fail_execution(receipt, code="REJECTED", message="test failure", retryable=False)
    type(doc.blob).objects.filter(pk=doc.blob.pk).update(sha256="new-source")
    assert list(scoped_recovery_blocked_node_ids()) == ([doc.node.pk] if running else [])
    # Changing source never restarts the old job as a new input.
    replay = claim(envelope, manifest)
    assert not replay.should_execute and replay.receipt.attempt == 1


def test_new_parse_revision_alone_does_not_release_failed_request(scoped, doc):
    envelope, manifest = scoped.configure()
    receipt = claim(envelope, manifest).receipt
    fail_execution(receipt, code="REJECTED", message="test failure", retryable=False)
    parsed(doc)
    assert list(scoped_recovery_blocked_node_ids()) == [doc.node.pk]


def test_legacy_job_cannot_claim_node_owned_by_running_scoped_job(scoped, doc):
    envelope, manifest = scoped.configure()
    scoped_receipt = claim(envelope, manifest).receipt
    legacy = claim_execution(job_id="legacy-attempt", work_kind="parse_document",
        target_key="node:" + str(doc.node.pk), payload={"node_id": doc.node.pk},
        source_snapshot={"node_ids": [doc.node.pk]})
    assert not legacy.should_execute and legacy.receipt.job_id == scoped_receipt.job_id
    assert not ExecutorJobReceipt.objects.filter(pk="legacy-attempt").exists()
