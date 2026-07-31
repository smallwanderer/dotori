import io
import json

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from document_ai.management.commands.server_status import (
    HEALTHCHECK_FILE_PREFIX,
    HEALTHCHECK_OWNER_EMAIL,
    _embedding_status,
    _rag_status,
    check_file_io_pipeline,
)
from llm_installation.catalog import get_catalog_entry
from llm_installation.config_store import write_llm_runtime_config
from llm_installation.runtime_probe import ServerRuntimeProfile
from llm_installation.router import resolve_server_rag_target
from files.models import Node
from files.services import storage as storage_service

pytestmark = pytest.mark.unit

User = get_user_model()

GGUF_MODEL_ID = "qwen2.5-7b-instruct-q4_k_m"


class FileIOPipelineCheckTests(TestCase):
    def test_happy_path_cleans_up_after_itself(self):
        result = check_file_io_pipeline()

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "ok")
        self.assertFalse(
            Node.objects.filter(name__startswith=HEALTHCHECK_FILE_PREFIX).exists()
        )
        owner = User.objects.get(email=HEALTHCHECK_OWNER_EMAIL)
        self.assertFalse(owner.is_active)
        self.assertFalse(owner.has_usable_password())

    def test_idempotent_across_repeated_runs(self):
        check_file_io_pipeline()
        check_file_io_pipeline()

        self.assertEqual(
            User.objects.filter(email=HEALTHCHECK_OWNER_EMAIL).count(), 1
        )
        self.assertFalse(
            Node.objects.filter(name__startswith=HEALTHCHECK_FILE_PREFIX).exists()
        )

    def test_sweeps_orphaned_file_from_previous_crashed_run(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        owner = User.objects.create(email=HEALTHCHECK_OWNER_EMAIL, is_active=False)
        stale_upload = SimpleUploadedFile(
            f"{HEALTHCHECK_FILE_PREFIX}stale.txt", b"leftover", content_type="text/plain"
        )
        storage_service.save_file(
            owner=owner, file=stale_upload, description="", ai_processing_enabled=False
        )
        self.assertTrue(
            Node.objects.filter(name__startswith=HEALTHCHECK_FILE_PREFIX).exists()
        )

        check_file_io_pipeline()

        self.assertFalse(
            Node.objects.filter(name__startswith=HEALTHCHECK_FILE_PREFIX).exists()
        )

    def test_failure_path_still_cleans_up_node(self):
        original_open_file = storage_service.open_file

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated read failure")

        storage_service.open_file = _boom
        try:
            result = check_file_io_pipeline()
        finally:
            storage_service.open_file = original_open_file

        self.assertFalse(result["ok"])
        self.assertIn("simulated read failure", result["message"])
        self.assertFalse(
            Node.objects.filter(name__startswith=HEALTHCHECK_FILE_PREFIX).exists()
        )


@pytest.mark.parametrize(
    "embedding_model_env,expected_enabled",
    [
        ("BAAI/bge-m3", True),
        ("", False),
        (None, False),
    ],
)
def test_embedding_status_enabled_matches_env_presence(
    monkeypatch, embedding_model_env, expected_enabled
):
    if embedding_model_env is None:
        monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    else:
        monkeypatch.setenv("EMBEDDING_MODEL", embedding_model_env)

    status = _embedding_status()

    assert status["enabled"] is expected_enabled
    # Detail fields always resolve via settings, even when disabled.
    assert status["model"]


@pytest.mark.parametrize(
    "query_llm_enabled_env,expected_enabled",
    [
        ("1", True),
        (None, True),
        ("0", False),
        ("false", False),
    ],
)
def test_rag_status_enabled_matches_query_llm_enabled_env(
    monkeypatch, tmp_path, query_llm_enabled_env, expected_enabled
):
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(tmp_path / "missing.json"))
    if query_llm_enabled_env is None:
        monkeypatch.delenv("QUERY_LLM_ENABLED", raising=False)
    else:
        monkeypatch.setenv("QUERY_LLM_ENABLED", query_llm_enabled_env)

    status = _rag_status(timeout=1)

    assert status["enabled"] is expected_enabled
    assert status["configured"] is False


def test_rag_status_reads_persisted_config_without_live_probe(monkeypatch, tmp_path):
    profile = ServerRuntimeProfile(
        cpu_count=8,
        ram_mb=32768,
        has_gpu=False,
        gpu_name="",
        gpu_vram_mb=0,
        gpu_vram_free_mb=0,
        gpu_count=0,
        gpu_vram_list=[],
        gpu_vram_free_list=[],
        cuda_available=False,
        platform="test",
        cluster_mode=False,
        disk_free_mb=102400,
    )
    catalog = [get_catalog_entry(GGUF_MODEL_ID)]
    target = resolve_server_rag_target(
        profile=profile,
        catalog=catalog,
        check_endpoint=False,
        priority_preset="balanced",
    )
    config_path = tmp_path / "llm_runtime.json"
    write_llm_runtime_config(target=target, profile=profile, catalog=catalog, path=config_path)
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("QUERY_LLM_ENABLED", "0")
    monkeypatch.setattr(
        "document_ai.management.commands.server_status.check_endpoint_health",
        lambda *a, **k: pytest.fail("must not probe network when RAG is disabled"),
    )

    status = _rag_status(timeout=1)

    assert status["enabled"] is False
    assert status["configured"] is True
    assert status["runtime"] == target.runtime


def test_rag_status_reports_persisted_oom_without_live_probe(monkeypatch, tmp_path):
    config_path = tmp_path / "llm_runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "target": {
                    "endpoint_name": "Local runtime",
                    "base_url": "http://rag-runtime:8080",
                    "model": "test-model",
                    "runtime": "llama.cpp",
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "runtime_status.json").write_text(
        json.dumps(
            {
                "status": "unavailable",
                "reason_code": "LLM_UNAVAILABLE_OOM",
                "retryable": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_RUNTIME_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("QUERY_LLM_ENABLED", "1")
    monkeypatch.setattr(
        "document_ai.management.commands.server_status.check_endpoint_health",
        lambda *a, **k: pytest.fail("must not probe an unavailable runtime"),
    )

    status = _rag_status(timeout=1)

    assert status["configured"] is True
    assert status["available"] is False
    assert status["runtime_status"]["reason_code"] == "LLM_UNAVAILABLE_OOM"


class ServerStatusCommandTests(TestCase):
    def test_json_output_contains_expected_top_level_keys(self):
        out = io.StringIO()
        call_command("server_status", "--json-output", "--skip-file-io", stdout=out)
        payload = json.loads(out.getvalue())

        assert set(payload.keys()) == {"connection", "features"}
        assert set(payload["features"].keys()) == {"file_io", "embedding", "rag"}
        assert payload["features"]["file_io"]["pipeline_check"] is None
