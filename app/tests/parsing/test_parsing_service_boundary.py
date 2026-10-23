from unittest.mock import patch

import pytest

@pytest.fixture(autouse=True)
def active_module_plan(tmp_path, monkeypatch):
    """Exercise transport/recovery against an explicit isolated installation."""
    from capabilities.deployment import clear_deployment_snapshot_cache
    from installation.deployment import build_module_deployment_plan, write_deployment_plan

    path = write_deployment_plan(build_module_deployment_plan(["documents"]), repo_root=tmp_path)
    monkeypatch.setenv("DOTORI_DEPLOYMENT_PLAN_PATH", str(path))
    clear_deployment_snapshot_cache()
    yield
    clear_deployment_snapshot_cache()


from celery.exceptions import Retry
from django.test import SimpleTestCase

from documents.tasks import parse_document_with_docling


class ParseTaskBoundaryTests(SimpleTestCase):
    def test_canonical_task_keeps_transport_contract_and_dispatches_executor(self):
        with patch("documents.tasks._embedding_queue_backpressure", return_value=(False, 0, 32)), patch(
            "documents.services.executor_transport.dispatch_executor_job",
            return_value={"ok": True, "result": {"status": "success", "node_id": 41, "chunk_count": 2}, "followups": []},
        ) as dispatch:
            result = parse_document_with_docling.run(
                41,
                trace_id="trace-41",
                enqueued_at="2026-09-06T12:00:00+09:00",
                job_id="parse-job-41",
                work_kind="parse_document",
                envelope_version=1,
            )

        self.assertEqual(result, {"status": "success", "node_id": 41, "chunk_count": 2})
        self.assertEqual(dispatch.call_args.args[0].value, "parse_document")
        self.assertEqual(dispatch.call_args.kwargs["payload"], {"node_id": 41})
        self.assertEqual(dispatch.call_args.kwargs["envelope"]["job_id"], "parse-job-41")

        self.assertEqual(parse_document_with_docling.name, "documents.tasks.parse_document_with_docling")
        self.assertEqual(parse_document_with_docling.queue, "parse")
        self.assertTrue(parse_document_with_docling.acks_late)
        self.assertTrue(parse_document_with_docling.reject_on_worker_lost)
        self.assertEqual(parse_document_with_docling.max_retries, 2)

    def test_retryable_executor_failure_uses_existing_celery_retry(self):
        from documents.services.executor_transport import ExecutorDispatchError

        with patch("documents.tasks._embedding_queue_backpressure", return_value=(False, 0, 32)), patch(
            "documents.services.executor_transport.dispatch_executor_job",
            side_effect=ExecutorDispatchError("executor unavailable"),
        ), patch.object(
            parse_document_with_docling, "retry", side_effect=Retry()
        ) as retry:
            with self.assertRaises(Retry):
                parse_document_with_docling.run(41, job_id="parse-job", work_kind="parse_document", envelope_version=1)

        self.assertIn("executor unavailable", str(retry.call_args.kwargs["exc"]))
