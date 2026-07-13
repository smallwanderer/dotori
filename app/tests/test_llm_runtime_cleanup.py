import json

import pytest

from document_ai.llm_installation_helper.catalog import get_catalog_entry, get_rag_model_catalog
from document_ai.llm_installation_helper.cleanup import (
    cleanup_stale_runtime,
    clear_runtime_config_files,
    extract_runtime_and_repo,
    get_runtime_cache_dir,
    hf_cache_dirname,
    remove_current_llm_runtime,
    remove_model_weights,
)
from document_ai.llm_installation_helper.config_store import write_llm_runtime_config
from document_ai.llm_installation_helper.router import resolve_server_rag_target
from document_ai.llm_installation_helper.runtime_probe import ServerRuntimeProfile

pytestmark = pytest.mark.unit

GGUF_MODEL_ID = "qwen2.5-7b-instruct-q4_k_m"
GGUF_REPO_ID = "Qwen/Qwen2.5-7B-Instruct-GGUF"


def _profile(*, ram_mb=32768, disk_free_mb=102400, gpu_vram_mb=0):
    gpu_vram_list = [] if not gpu_vram_mb else [gpu_vram_mb]
    return ServerRuntimeProfile(
        cpu_count=8,
        ram_mb=ram_mb,
        has_gpu=bool(gpu_vram_mb),
        gpu_name="Test GPU" if gpu_vram_mb else "",
        gpu_vram_mb=gpu_vram_mb,
        gpu_vram_free_mb=gpu_vram_mb,
        gpu_count=len(gpu_vram_list),
        gpu_vram_list=gpu_vram_list,
        gpu_vram_free_list=list(gpu_vram_list),
        cuda_available=bool(gpu_vram_mb),
        platform="test",
        cluster_mode=False,
        disk_free_mb=disk_free_mb,
    )


def _write_config(tmp_path, model_id=GGUF_MODEL_ID):
    profile = _profile()
    catalog = [get_catalog_entry(model_id)]
    target = resolve_server_rag_target(
        profile=profile,
        catalog=catalog,
        check_endpoint=False,
        priority_preset="balanced",
    )
    config_path = write_llm_runtime_config(
        target=target,
        profile=profile,
        catalog=catalog,
        path=tmp_path / "data" / "config" / "llm_runtime.json",
    )
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return config_path, payload


def test_extract_runtime_and_repo_reads_persisted_config(tmp_path):
    _, payload = _write_config(tmp_path)

    info = extract_runtime_and_repo(payload)

    assert info is not None
    runtime, repo_id = info
    assert runtime == "llama.cpp"
    assert repo_id.startswith("Qwen/Qwen2.5-7B-Instruct-GGUF")


def test_extract_runtime_and_repo_returns_none_for_empty_payload():
    assert extract_runtime_and_repo({}) is None
    assert extract_runtime_and_repo({"target": {}}) is None


def test_remove_model_weights_deletes_cached_dir_and_is_idempotent(tmp_path):
    cache_dir = get_runtime_cache_dir("llama.cpp", tmp_path)
    model_dir = cache_dir / hf_cache_dirname(GGUF_REPO_ID)
    model_dir.mkdir(parents=True)
    (model_dir / "weights.bin").write_bytes(b"fake")

    removed, _ = remove_model_weights("llama.cpp", GGUF_REPO_ID, tmp_path)
    assert removed is True
    assert not model_dir.exists()

    removed_again, message = remove_model_weights("llama.cpp", GGUF_REPO_ID, tmp_path)
    assert removed_again is False
    assert "No cached weights" in message


def test_clear_runtime_config_files_removes_config_and_args(tmp_path):
    config_path, _ = _write_config(tmp_path)
    assert config_path.exists()
    assert config_path.with_name("llama_rag.args").exists()

    removed = clear_runtime_config_files(config_path)

    assert config_path in removed
    assert config_path.with_name("llama_rag.args") in removed
    assert not config_path.exists()
    assert not config_path.with_name("llama_rag.args").exists()


def test_cleanup_stale_runtime_noop_when_model_unchanged(tmp_path):
    _, payload = _write_config(tmp_path)
    runtime, repo_id = extract_runtime_and_repo(payload)

    cache_dir = get_runtime_cache_dir(runtime, tmp_path)
    model_dir = cache_dir / hf_cache_dirname(repo_id)
    model_dir.mkdir(parents=True)

    messages = cleanup_stale_runtime(payload, runtime, repo_id, repo_root=tmp_path)

    assert messages == []
    assert model_dir.exists()


def test_cleanup_stale_runtime_removes_old_weights_when_model_changes(tmp_path):
    _, payload = _write_config(tmp_path)
    runtime, repo_id = extract_runtime_and_repo(payload)

    cache_dir = get_runtime_cache_dir(runtime, tmp_path)
    model_dir = cache_dir / hf_cache_dirname(repo_id)
    model_dir.mkdir(parents=True)

    messages = cleanup_stale_runtime(
        payload, runtime, "some-org/some-other-model", repo_root=tmp_path
    )

    assert not model_dir.exists()
    assert any("Removed cached weights" in message for message in messages)


def test_remove_current_llm_runtime_clears_config_when_present(tmp_path, monkeypatch):
    config_path, _ = _write_config(tmp_path)
    monkeypatch.setattr(
        "document_ai.llm_installation_helper.cleanup.stop_and_remove_container",
        lambda service, repo_root=None: True,
    )

    result = remove_current_llm_runtime(repo_root=tmp_path, config_path=config_path)

    assert result["had_config"] is True
    assert not config_path.exists()


def test_remove_current_llm_runtime_handles_missing_config(tmp_path):
    result = remove_current_llm_runtime(
        repo_root=tmp_path, config_path=tmp_path / "data" / "config" / "llm_runtime.json"
    )

    assert result["had_config"] is False
    assert "No resolvable runtime/model" in result["messages"][0]
