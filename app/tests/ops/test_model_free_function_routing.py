import copy
import json

import pytest

from capabilities.deployment import DeploymentSnapshotError, FunctionRouteError, load_deployment_snapshot
from installation.deployment import build_module_deployment_plan

pytestmark = pytest.mark.unit


def read(tmp_path, plan):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return load_deployment_snapshot(path=path)


def test_parser_registered_without_fake_model_and_selected_with_provenance(tmp_path):
    plan = build_module_deployment_plan(["documents"])
    assert plan["schema_version"] == 4
    assert plan["installed_models"] == {}
    assert "document.extract" not in plan["default_routes"]
    snapshot = read(tmp_path, plan)
    with pytest.raises(FunctionRouteError):
        snapshot.resolve_function_provider("document.extract")
    selected = snapshot.resolve_function_provider("document.extract", explicit_provider_installation_id="parser:docling:1")
    assert selected["provider_installation_id"] == "parser:docling:1"
    assert selected["declaration_ref"] == "dotori-parser-http-v1"
    assert selected["contract_version"] == 1
    assert "model_installation_id" not in selected


@pytest.mark.parametrize("field,value", [
    ("contract_version", 2), ("contract_version", True),
    ("input_schema", "other"), ("output_schema", "other"),
    ("adapter_contract", "other"), ("preferred_role", "llm"),
    ("provider_installation_id", "missing"), ("role", "llm"),
    ("verification", "contract_verified"), ("declaration_ref", "unknown"),
    ("provider_installation_id", ["invalid"]),
])
def test_reader_rejects_incompatible_parser_contract(tmp_path, field, value):
    plan = build_module_deployment_plan(["documents"])
    plan["function_providers"]["document.extract"][0][field] = value
    with pytest.raises(DeploymentSnapshotError):
        read(tmp_path, plan)


@pytest.mark.parametrize("fault", ["mixed", "unknown", "duplicate", "binding", "registration", "default"])
def test_reader_rejects_invalid_provider_registration(tmp_path, fault):
    plan = build_module_deployment_plan(["documents"])
    candidate = plan["function_providers"]["document.extract"][0]
    if fault == "mixed":
        candidate["model_installation_id"] = "fake"
    elif fault == "unknown":
        plan["function_providers"]["other.function"] = []
    elif fault == "duplicate":
        plan["function_providers"]["document.extract"].append(copy.deepcopy(candidate))
    elif fault == "binding":
        plan["role_bindings"]["parser"]["provider_installation_id"] = "missing"
    elif fault == "registration":
        plan["installed_providers"]["parser:docling:1"]["registration_revision"] = "other"
    else:
        plan["default_routes"]["document.extract"] = "parser:docling:1"
    with pytest.raises(DeploymentSnapshotError):
        read(tmp_path, plan)


def test_disabled_parser_cannot_be_explicitly_selected(tmp_path):
    snapshot = read(tmp_path, build_module_deployment_plan(["documents"], disabled_provider_roles=("parser", "embedding")))
    with pytest.raises(FunctionRouteError, match="disabled"):
        snapshot.resolve_function_provider("document.extract", explicit_provider_installation_id="parser:docling:1")


@pytest.mark.parametrize("schema", [2, 3])
def test_old_schema_reads_without_inventing_function_verification(tmp_path, schema):
    plan = build_module_deployment_plan(["documents"])
    plan["schema_version"] = schema
    plan.pop("installed_providers")
    plan["role_bindings"]["parser"].pop("provider_installation_id")
    plan["function_providers"] = {}
    if schema == 2:
        plan.pop("role_bindings")
    snapshot = read(tmp_path, plan)
    assert snapshot.installed_providers == {}
    with pytest.raises(FunctionRouteError):
        snapshot.resolve_function_provider("document.extract")


def test_core_only_has_no_model_free_provider(tmp_path):
    snapshot = read(tmp_path, build_module_deployment_plan([]))
    assert snapshot.installed_providers == {}
    assert snapshot.function_providers == {}


def test_dual_explicit_selection_rejected(tmp_path):
    snapshot = read(tmp_path, build_module_deployment_plan(["documents"]))
    with pytest.raises(FunctionRouteError, match="either"):
        snapshot.resolve_function_provider("document.extract", explicit_model_installation_id="model", explicit_provider_installation_id="parser:docling:1")


def test_planner_enriches_model_candidate_without_mutating_input():
    candidate = {"model_installation_id": "llm:test", "role": "llm",
                 "verification": "declared_unverified", "declaration_ref": "test-declaration",
                 "adapter_contract": "openai-compatible-chat-completions-v1"}
    original = copy.deepcopy(candidate)
    plan = build_module_deployment_plan(["documents"], installed_models={"llm:test": {
        "role": "llm", "catalog_id": "test", "artifact_revision": "1", "runtime_ref": "llm_runtime.json"}},
        function_providers={"text.generate": [candidate]})
    assert candidate == original
    assert plan["function_providers"]["text.generate"][0]["input_schema"] == "dotori.chat.messages.v1"


@pytest.mark.parametrize("field,value", [("contract_version", 2), ("adapter_contract", "other"),
                                         ("output_schema", "other")])
def test_planner_rejects_incompatible_model_candidate(field, value):
    candidate = {"model_installation_id": "llm:test", "role": "llm",
                 "verification": "declared_unverified", "declaration_ref": "test-declaration",
                 "adapter_contract": "openai-compatible-chat-completions-v1", field: value}
    with pytest.raises(ValueError, match="incompatible"):
        build_module_deployment_plan(["documents"], installed_models={"llm:test": {
            "role": "llm", "catalog_id": "test", "artifact_revision": "1", "runtime_ref": "llm_runtime.json"}},
            function_providers={"text.generate": [candidate]})
