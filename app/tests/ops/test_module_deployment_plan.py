from pathlib import Path

import pytest

from capabilities.modules import DOCUMENTS_MODULE, get_module, resolve_modules
from capabilities.registry import DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH
from capabilities.deployment import load_deployment_snapshot
from llm_installation.catalog import get_catalog_entry
from llm_installation.runtime_lifecycle import RuntimeSpec
from installation.deployment import (
    CORE_SERVICES,
    build_legacy_module_deployment_plan,
    build_module_deployment_plan,
    reusable_verified_llm_projection,
    write_deployment_plan,
)

pytestmark = pytest.mark.unit


def test_documents_manifest_declares_reusable_contract_and_default_providers():
    documents = get_module(DOCUMENTS_MODULE)

    assert documents.capabilities == (DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH)
    assert documents.execution_roles == ("parser", "embedding", "llm")
    assert all(provider.default_enabled for provider in documents.providers)
    assert set(documents.work_kinds) == {
        "embed_document",
        "parse_document", "plan_document_embedding",
        "embed_document_batch", "evaluate_retrieval",
    }


def test_module_selection_rejects_unknown_module():
    with pytest.raises(ValueError, match="Unknown modules: images"):
        resolve_modules(["images"])


def test_documents_selection_builds_complete_default_plan():
    plan = build_module_deployment_plan([DOCUMENTS_MODULE])

    assert plan["selected_modules"] == [DOCUMENTS_MODULE]
    assert plan["installed_capabilities"] == [DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH]
    assert plan["enabled_capabilities"] == plan["installed_capabilities"]
    assert set(plan["provider_bindings"]) == {"parser", "embedding", "llm"}
    assert all(binding["enabled"] for binding in plan["provider_bindings"].values())
    assert plan["services"] == [*CORE_SERVICES, "parser-executor", "embedding-executor"]
    assert plan["queues"] == ["parse", "embed"]
    assert set(plan["work_routes"]) == {
        "embed_document",
        "parse_document", "plan_document_embedding",
        "embed_document_batch", "evaluate_retrieval",
    }


def test_core_only_selection_has_no_optional_cost():
    plan = build_module_deployment_plan([])

    assert plan["selected_modules"] == []
    assert plan["installed_capabilities"] == []
    assert plan["enabled_capabilities"] == []
    assert plan["provider_bindings"] == {}
    assert plan["services"] == list(CORE_SERVICES)
    assert plan["queues"] == []
    assert plan["work_routes"] == {}


@pytest.mark.parametrize(
    "disabled,enabled_capabilities,roles,routes",
    [
        (("llm",), (DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH), {"parser", "embedding"}, {"parse_document", "embed_document", "plan_document_embedding", "embed_document_batch", "evaluate_retrieval"}),
        (("embedding",), (DOCUMENT_EXTRACT,), {"parser", "llm"}, {"parse_document"}),
        (("embedding", "llm"), (DOCUMENT_EXTRACT,), {"parser"}, {"parse_document"}),
    ],
)
def test_provider_can_be_disabled_without_uninstalling_documents(
    disabled, enabled_capabilities, roles, routes
):
    plan = build_module_deployment_plan([DOCUMENTS_MODULE], disabled_provider_roles=disabled)

    assert plan["selected_modules"] == [DOCUMENTS_MODULE]
    assert plan["installed_capabilities"] == [DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH]
    assert tuple(plan["enabled_capabilities"]) == enabled_capabilities
    assert set(plan["execution_roles"]) == roles
    assert set(plan["work_routes"]) == routes


def test_module_plan_is_stable_for_duplicate_selection():
    first = build_module_deployment_plan([DOCUMENTS_MODULE])
    duplicate = build_module_deployment_plan([DOCUMENTS_MODULE, DOCUMENTS_MODULE])

    assert duplicate == first


@pytest.mark.parametrize(
    "mode,modules,enabled_roles",
    [
        ("basic", [], set()),
        ("extract", [DOCUMENTS_MODULE], {"parser"}),
        ("search", [DOCUMENTS_MODULE], {"parser", "embedding"}),
        ("rag", [DOCUMENTS_MODULE], {"parser", "embedding", "llm"}),
    ],
)
def test_legacy_mode_is_only_an_adapter_to_module_selection(
    mode, modules, enabled_roles
):
    plan = build_legacy_module_deployment_plan(mode)

    assert plan["selected_modules"] == modules
    assert set(plan["execution_roles"]) == enabled_roles
    assert "mode" not in plan


def test_module_plan_can_be_persisted(tmp_path):
    plan = build_module_deployment_plan([DOCUMENTS_MODULE])

    path = write_deployment_plan(plan, repo_root=tmp_path)

    assert path.exists()
    assert path.read_text(encoding="utf-8").endswith("\n")


def _llm_runtime_with_catalog_entry(backend_id: str) -> dict:
    entry = get_catalog_entry("gemma-3-4b-it-qat-q4_0-gguf")
    assert entry is not None
    return {
        "target": {
            "model": entry.id,
            "generation_id": "llm-generation-test",
            "runtime_policy_input": {
                "catalog_assessment": {
                    "backend_profile": backend_id,
                    "catalog_entry": entry.model_dump(mode="json"),
                }
            },
            "serving_profile": {"backend_profile": backend_id},
        }
    }


def test_deployment_plan_projects_catalog_evidence_for_exact_llm_backend(tmp_path):
    plan = build_legacy_module_deployment_plan(
        "rag",
        llm_runtime_config=_llm_runtime_with_catalog_entry("llamacpp-cpu"),
    )

    installation_id = "llm:llm-generation-test"
    model = plan["installed_models"][installation_id]
    candidate = plan["function_providers"]["text.generate"][0]

    assert model["catalog_id"] == "gemma-3-4b-it-qat-q4_0-gguf"
    assert model["artifact_revision"] == "15f73f5eee9c28f53afefef5723e29680c2fc78a"
    assert model["artifact_digest"] == "76aed0a8285b83102f18b5d60e53c70d09eb4e9917a20ce8956bd546452b56e2"
    assert model["backend_id"] == "llamacpp-cpu"
    assert candidate["verification"] == "contract_verified"
    assert candidate["evidence_ref"] == "text-generate-gemma3-qat-q4_0-llamacpp-cpu-20260930-command"
    assert candidate["backend_id"] == "llamacpp-cpu"
    assert plan["default_routes"]["text.generate"] == installation_id

    path = write_deployment_plan(plan, repo_root=tmp_path)
    snapshot = load_deployment_snapshot(path=path)
    resolved = snapshot.resolve_function_provider("text.generate")
    assert resolved["model_installation_id"] == installation_id
    assert resolved["evidence_ref"] == candidate["evidence_ref"]


def test_deployment_plan_does_not_project_evidence_for_an_untested_backend():
    plan = build_legacy_module_deployment_plan(
        "rag",
        llm_runtime_config=_llm_runtime_with_catalog_entry(
            "llamacpp-gpu-offload"
        ),
    )

    assert "text.generate" not in plan["function_providers"]
    assert "text.generate" not in plan["default_routes"]


def test_deployment_rebuild_carries_verified_route_for_same_immutable_runtime():
    runtime = RuntimeSpec(
        scope="production",
        runtime="llama.cpp",
        model_id="gemma-3-4b-it-qat-q4_0-gguf",
        image="dotori/llama-rag",
        container_name="dotori-llm",
        network_name="dotori-runtime",
        network_alias="rag-runtime",
        generation_id="llm-generation-test",
        args_file=Path("/tmp/runtime.args"),
        health_url="http://localhost:8080/health",
        model_probe_url="http://localhost:8080/v1/models",
    )
    previous = build_legacy_module_deployment_plan(
        "rag",
        runtime=runtime,
        llm_runtime_config=_llm_runtime_with_catalog_entry("llamacpp-cpu"),
    )
    stale_runtime = _llm_runtime_with_catalog_entry("llamacpp-cpu")
    stale_runtime["target"]["runtime_policy_input"]["catalog_assessment"][
        "catalog_entry"
    ]["functions"] = []

    projection = reusable_verified_llm_projection(
        previous,
        stale_runtime,
        mode="rag",
    )

    rebuilt = build_legacy_module_deployment_plan(
        "rag",
        runtime=runtime,
        llm_runtime_config=stale_runtime,
        **projection,
    )
    assert rebuilt["installed_models"] == previous["installed_models"]
    assert rebuilt["function_providers"] == previous["function_providers"]
    assert rebuilt["default_routes"]["text.generate"] == "llm:llm-generation-test"


def test_deployment_rebuild_does_not_carry_route_across_runtime_or_role_change():
    runtime_config = _llm_runtime_with_catalog_entry("llamacpp-cpu")
    previous = {
        "schema_version": 3,
        "mode": "rag",
        "runtime_generation": "old-generation",
        "installed_models": {
            "llm:old-generation": {
                "role": "llm",
                "catalog_id": runtime_config["target"]["model"],
                "artifact_revision": "pinned-revision",
                "artifact_digest": "sha256",
                "backend_id": "llamacpp-cpu",
                "runtime_ref": "llm_runtime.json",
            }
        },
        "function_providers": {
            "text.generate": [
                {
                    "model_installation_id": "llm:old-generation",
                    "role": "llm",
                    "verification": "contract_verified",
                    "evidence_ref": "evidence",
                    "backend_id": "llamacpp-cpu",
                }
            ]
        },
        "default_routes": {"text.generate": "llm:old-generation"},
    }

    assert reusable_verified_llm_projection(
        previous,
        runtime_config,
        mode="rag",
    ) == {}
    assert reusable_verified_llm_projection(
        previous,
        runtime_config,
        mode="search",
    ) == {}


def test_verified_embedding_projection_preserves_runtime_identity(tmp_path):
    import json
    from documents.services.embedding_runtime_config import load_embedding_runtime
    from llm_installation.embedding_catalog.loader import get_embedding_catalog_entry
    from llm_installation.embedding_config_store import build_embedding_runtime_payload
    entry = get_embedding_catalog_entry("bge-m3-hybrid")
    assert entry.functions[0].verification == "contract_verified"
    # Project new catalog declarations without rewriting the serving runtime.
    runtime = build_embedding_runtime_payload(entry=entry, scope="production", generation_id="test-embedding")
    plan = build_module_deployment_plan([DOCUMENTS_MODULE], embedding_runtime_config=runtime)
    path = write_deployment_plan(plan, repo_root=tmp_path)
    candidate = load_deployment_snapshot(path=path).resolve_function_provider("text.embed")
    assert candidate["model_installation_id"] == "embedding:test-embedding"
    assert candidate["evidence_ref"] == entry.functions[0].evidence_ref
