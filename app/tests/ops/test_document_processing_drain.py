from unittest.mock import Mock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from document_ai.models import ExecutorJobReceipt
from document_ai.processing.drain import document_drain_snapshot


pytestmark = pytest.mark.unit


@pytest.mark.django_db
def test_drain_snapshot_counts_document_receipts_and_broker_queues():
    ExecutorJobReceipt.objects.create(
        job_id="parse-active",
        work_kind="parse_document",
        target_key="node:1",
    )
    ExecutorJobReceipt.objects.create(
        job_id="unrelated",
        work_kind="future_non_document_work",
        target_key="other:1",
    )
    redis = Mock()
    redis.llen.side_effect = [2, 3]

    snapshot = document_drain_snapshot(redis_client=redis)

    assert snapshot.active_receipts == 1
    assert snapshot.parse_queue_depth == 2
    assert snapshot.embed_queue_depth == 3
    assert snapshot.pending_work == 6


def test_wait_command_requires_two_consecutive_empty_observations():
    pending = Mock(pending_work=1, active_receipts=1, parse_queue_depth=0, embed_queue_depth=0)
    empty = Mock(pending_work=0, active_receipts=0, parse_queue_depth=0, embed_queue_depth=0)

    with patch(
        "document_ai.management.commands.wait_for_document_processing.document_drain_snapshot",
        side_effect=[pending, empty, empty],
    ), patch("document_ai.management.commands.wait_for_document_processing.time.sleep"):
        call_command("wait_for_document_processing", timeout=10, poll_interval=0.05)


def test_wait_command_fails_closed_when_state_cannot_be_inspected():
    with patch(
        "document_ai.management.commands.wait_for_document_processing.document_drain_snapshot",
        side_effect=RuntimeError("redis unavailable"),
    ):
        with pytest.raises(CommandError, match="Unable to inspect"):
            call_command("wait_for_document_processing", timeout=0)


def test_wait_command_times_out_while_active_work_remains():
    pending = Mock(
        pending_work=1,
        active_receipts=1,
        parse_queue_depth=0,
        embed_queue_depth=0,
    )

    with patch(
        "document_ai.management.commands.wait_for_document_processing.document_drain_snapshot",
        return_value=pending,
    ), patch(
        "document_ai.management.commands.wait_for_document_processing.time.monotonic",
        side_effect=[0.0, 1.0],
    ):
        with pytest.raises(CommandError, match="active=1"):
            call_command("wait_for_document_processing", timeout=1)
