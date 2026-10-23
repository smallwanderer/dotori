"""Opt-in real Redis/Celery/TCP test; parser/model remain deterministic fakes."""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4
from unittest.mock import patch

import pytest
from django.db import close_old_connections
from django.test import RequestFactory

from documents.models import DocumentParseResult, ExecutorJobReceipt
from orchestration import executor_views
from orchestration.submissions import PlanDelivery
from tests.processing.test_embedding_execution_delivery import submit
from tests.processing.test_embedding_preparation import deployment, doc, parsed
from tests.processing.test_embedding_plan_journal import admitted
from tests.processing.test_embedding_manifests import approval
from tests.embedding.test_scoped_embedding_execution import scoped, embedding
from tests.embedding.test_embedding_persistence import isolated_runtime_cache

pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.skipif(
    os.getenv("DOTORI_TEST_LIVE_TRANSPORT") != "1", reason="Opt-in isolated Redis/Celery/TCP integration")]


@pytest.mark.parametrize("needs_parse,lose_report", [(False, False), (True, False), (False, True)])
def test_real_queue_http_and_receipt_report_recovery(
        doc, admitted, scoped, settings, monkeypatch, needs_parse, lose_report):
    from celery.contrib.testing.worker import start_worker
    from celery import Celery, current_app
    from kombu import pools
    from documents.tasks import embed_document_scoped, parse_document_with_docling
    from redis import Redis

    settings.EXECUTOR_INTERNAL_TOKEN = "live-test-token"
    prefix = "dotori-test-transport:" + uuid4().hex + ":"
    redis = Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=2, socket_timeout=2)
    redis.ping()  # Explicit opt-in must fail, not skip, if transport is unavailable.
    previous_app = current_app._get_current_object()
    # Kombu global pools key connections by broker identity, not all transport
    # options. Reset this test process's pools before changing the Redis prefix.
    pools.reset()
    # A fresh app also isolates AMQP's cached producer pool, not just the
    # connection pool. No cached connection may escape into another namespace.
    app = Celery("dotori-transport-test-" + uuid4().hex,
        broker=settings.CELERY_BROKER_URL, backend="cache+memory://", fixups=[])
    # Avoid Django's lazy/namespace settings reload overriding isolation options.
    app.conf.update(broker_transport_options={"global_keyprefix": prefix,
        "visibility_timeout": 3600}, task_ignore_result=True, task_serializer="json",
        accept_content=["json"])
    app.finalize()  # The already imported shared tasks register on this fresh app.
    assert app.amqp.producer_pool.connections.connection.transport_options["global_keyprefix"] == prefix
    monkeypatch.setattr(embed_document_scoped._get_current_object(), "ignore_result", True)
    monkeypatch.setattr(parse_document_with_docling._get_current_object(), "ignore_result", True)
    requests_seen = []
    failures_injected = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.handle_job("POST")

        def do_GET(self):
            self.handle_job("GET")

        def log_message(self, *args):
            pass

        def handle_job(self, method):
            role = self.path.split("/")[1]
            requests_seen.append((method, role))
            close_old_connections()
            try:
                auth = self.headers.get("Authorization", "")
                if method == "POST":
                    body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    request = RequestFactory().post(self.path, body, content_type="application/json",
                        HTTP_AUTHORIZATION=auth)
                    with patch("orchestration.executor_views._executor_role", return_value=role):
                        response = executor_views.execute_job(request)
                else:
                    request = RequestFactory().get(self.path, HTTP_AUTHORIZATION=auth)
                    with patch("orchestration.executor_views._executor_role", return_value=role):
                        response = executor_views.job_status(request, self.path.rstrip("/").split("/")[-1])
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)
            finally:
                close_old_connections()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    monkeypatch.setattr("orchestration.transport.executor_url",
        lambda role: f"http://127.0.0.1:{server.server_port}/{role}")

    def parse_node(node_id, *, on_persisted, **kwargs):
        assert node_id == doc.node.pk
        result = parsed(doc)
        result.performance_metrics = {"job_id": kwargs["job_id"]}
        result.save(update_fields=["performance_metrics"])
        summary = {"status": "success", "node_id": node_id, "chunk_count": 2}
        on_persisted(summary)
        return summary

    real_record = PlanDelivery.record_response
    def record(delivery, kind, envelope, payload, response):
        if lose_report and kind == "embed_document" and not failures_injected:
            failures_injected.append(envelope["job_id"])
            raise RuntimeError("Test report commit interruption")
        return real_record(delivery, kind, envelope, payload, response)

    if needs_parse:
        DocumentParseResult.objects.filter(node=doc.node).delete()
    server_thread.start()
    try:
        with patch("documents.processing.parsing.get_parser_for_execution", return_value=object()), \
             patch("documents.processing.parsing.execute_parse_node", side_effect=parse_node) as parser, \
             patch("documents.embedding.execution.run_embedding_with_admission",
                   side_effect=lambda _, texts: embedding(texts)) as inference, \
             patch.object(PlanDelivery, "record_response", record), \
             start_worker(app, pool="solo", concurrency=1, queues=("parse", "embed"),
                          perform_ping_check=False, shutdown_timeout=10):
            row = submit(doc, mode="refresh")
            assert current_app._get_current_object() is app
            timeout = time.monotonic() + 90
            while time.monotonic() < timeout:
                row.refresh_from_db()
                if row.control.get("outcome") == "completed":
                    break
                time.sleep(0.2)
            assert row.control.get("outcome") == "completed", (row.reports, row.publish_error, requests_seen)
            receipt = ExecutorJobReceipt.objects.get(work_kind="embed_document")
            assert receipt.status == "succeeded" and receipt.attempt == 1
            assert len(row.reports) == (2 if needs_parse else 1)
            assert parser.call_count == int(needs_parse)
            assert inference.call_count == 2
            assert requests_seen.count(("POST", "embedding")) == 1
            assert requests_seen.count(("POST", "parser")) == int(needs_parse)
            if lose_report:
                assert failures_injected == [receipt.job_id]
                assert requests_seen.count(("GET", "embedding")) >= 1
            assert scoped.store.base_queryset().count() == 2
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        app.close()
        pools.reset()
        previous_app.set_current()
        previous_app.set_default()
        # Delete only the uniquely named test broker namespace, never a DB flush.
        for key in redis.scan_iter(match=prefix + "*"):
            assert key.startswith(prefix.encode())
            redis.delete(key)
        redis.close()
