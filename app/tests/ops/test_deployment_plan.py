import json
from dataclasses import replace

import pytest

from installation.deployment import (
    CORE_SERVICES,
    DeploymentPlan,
    WorkerSpec,
    build_deployment_plan,
    compose_up_command,
    read_deployment_plan,
    write_deployment_plan,
)
from llm_installation.runtime_lifecycle import build_runtime_spec


pytestmark = pytest.mark.unit


def test_fast_start_command_explicitly_disables_builds():
    command = compose_up_command(
        "docker compose -f docker-compose.yml",
        ["db", "redis", "app"],
        build_images=False,
    )

    assert command == (
        "docker compose -f docker-compose.yml up --no-build --remove-orphans -d db redis app"
    )


def test_maintenance_rebuild_command_builds_and_recreates():
    command = compose_up_command(
        "docker compose -f docker-compose.yml",
        ["db", "redis", "app"],
        build_images=True,
        force_recreate=True,
    )

    assert command == (
        "docker compose -f docker-compose.yml up --build --remove-orphans --force-recreate "
        "-d db redis app"
    )


def _runtime(tmp_path, scope="production"):
    return build_runtime_spec(
        scope,
        "llama.cpp",
        "test-model",
        "runtime-gen-1",
        repo_root=tmp_path,
    )


@pytest.mark.parametrize(
    "mode,enabled,disabled",
    [
        (
            "basic",
            (),
            ("dotori-document",),
        ),
        (
            "search",
            ("dotori-document",),
            (),
        ),
    ],
)
def test_plan_selects_workers_for_non_rag_modes(mode, enabled, disabled):
    plan = build_deployment_plan(mode)

    assert plan.core_services == CORE_SERVICES
    assert tuple(worker.compose_service for worker in plan.enabled_workers) == enabled
    assert plan.disabled_worker_services == disabled
    assert plan.runtime is None


def test_rag_plan_keeps_only_the_document_processing_worker(tmp_path):
    pending = build_deployment_plan("1")
    active = build_deployment_plan("rag", runtime=_runtime(tmp_path))

    assert pending.enabled_services == (*CORE_SERVICES, "dotori-document")
    assert active.enabled_services == (*CORE_SERVICES, "dotori-document")
    assert active.runtime is not None
    assert active.runtime.scope == active.scope


def test_plan_rejects_runtime_from_another_scope(tmp_path):
    runtime = replace(_runtime(tmp_path), scope="other")

    with pytest.raises(ValueError, match="scope"):
        build_deployment_plan("rag", scope="production", runtime=runtime)


def test_plan_rejects_runtime_in_search_mode(tmp_path):
    with pytest.raises(ValueError, match="Only RAG mode"):
        build_deployment_plan("search", runtime=_runtime(tmp_path))


def test_runtime_dependent_worker_cannot_be_enabled_without_runtime():
    rag_worker = WorkerSpec(
        name="rag",
        compose_service="runtime-worker",
        queues=("rag",),
        concurrency=1,
        prefetch_multiplier=1,
        enabled=True,
        requires_runtime=True,
        dependencies=("rag-runtime",),
        health_strategy="celery-ping",
    )

    with pytest.raises(ValueError, match="requires a RuntimeSpec"):
        DeploymentPlan(
            scope="production",
            mode="rag",
            core_services=CORE_SERVICES,
            workers=(rag_worker,),
            runtime=None,
            network_access="local",
            generation_id="plan-test",
        )


def test_plan_write_is_readable_and_stable(tmp_path):
    plan = build_deployment_plan("rag", runtime=_runtime(tmp_path))

    path = write_deployment_plan(plan, repo_root=tmp_path)
    first_payload = json.loads(path.read_text(encoding="utf-8"))
    write_deployment_plan(plan, repo_root=tmp_path)

    assert read_deployment_plan("production", repo_root=tmp_path) == first_payload
    assert first_payload["generation_id"] == plan.generation_id
    assert first_payload["runtime"]["args_file"].endswith("runtime.args")
