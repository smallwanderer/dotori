"""Validated, server-wide view of the active deployment plan."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from capabilities.functions import get_function, candidate_identity, validate_function_contract, validate_provider_registration
from capabilities.config import build_capability_config

DEPLOYMENT_PLAN_SCHEMA_VERSION = 4
SUPPORTED_DEPLOYMENT_PLAN_SCHEMA_VERSIONS = {2, 3, DEPLOYMENT_PLAN_SCHEMA_VERSION}


class DeploymentSnapshotError(ValueError):
    pass


class FunctionRouteError(ValueError):
    pass


@dataclass(frozen=True)
class DeploymentSnapshot:
    schema_version: int
    generation_id: str
    capability_revision: str
    selected_modules: tuple[str, ...]
    installed_capabilities: tuple[str, ...]
    enabled_capabilities: tuple[str, ...]
    role_bindings: dict[str, dict]
    execution_roles: dict[str, dict]
    services: tuple[str, ...]
    queues: tuple[str, ...]
    work_routes: dict[str, dict]
    installed_models: dict[str, dict]
    function_providers: dict[str, list[dict]]
    default_routes: dict[str, str]
    installed_providers: dict[str, dict] = field(default_factory=dict)

    @property
    def provider_bindings(self) -> dict[str, dict]:
        """Compatibility alias for callers written against schema 2."""
        return self.role_bindings

    def is_installed(self, capability_id: str) -> bool:
        return capability_id in self.installed_capabilities

    def is_enabled(self, capability_id: str) -> bool:
        return capability_id in self.enabled_capabilities

    def provider_enabled(self, role: str) -> bool:
        binding = self.role_bindings.get(role)
        return bool(isinstance(binding, dict) and binding.get("enabled"))

    def resolve_function_provider(
        self,
        function_id: str,
        *,
        explicit_model_installation_id: str | None = None,
        preferred_role: str | None = None,
        explicit_provider_installation_id: str | None = None,
    ) -> dict:
        """Select one enabled local model for a declared function contract."""
        candidates = self.function_providers.get(function_id, [])
        if explicit_model_installation_id and explicit_provider_installation_id:
            raise FunctionRouteError("Select either a model or a model-free provider")
        explicit_id = explicit_model_installation_id or explicit_provider_installation_id
        reference_key = "provider_installation_id" if explicit_provider_installation_id else "model_installation_id"
        if explicit_id:
            selected = next(
                (
                    candidate for candidate in candidates
                    if candidate.get(reference_key) == explicit_id
                ),
                None,
            )
            if selected is None:
                raise FunctionRouteError(
                    f"Selected model does not declare function {function_id}"
                )
            if not self.provider_enabled(str(selected.get("role") or "")):
                raise FunctionRouteError("Selected function provider is disabled")
            return selected

        default_model = self.default_routes.get(function_id)
        if default_model:
            selected = next(
                (
                    candidate for candidate in candidates
                    if (candidate.get("model_installation_id") or candidate.get("provider_installation_id")) == default_model
                ),
                None,
            )
            if selected is not None:
                if not self.provider_enabled(str(selected.get("role") or "")):
                    raise FunctionRouteError("Default function provider is disabled")
                return selected

        if self.schema_version >= 4 and preferred_role is None:
            preferred_role = get_function(function_id).preferred_role
        eligible = [
            candidate for candidate in candidates
            if candidate.get("verification") == "quality_verified"
            or (
                function_id in {"text.generate", "text.embed"}
                and candidate.get("verification") == "contract_verified"
            )
        ]
        eligible = [
            candidate for candidate in eligible
            if self.provider_enabled(str(candidate.get("role") or ""))
        ]
        if preferred_role:
            matching = [candidate for candidate in eligible if candidate.get("role") == preferred_role]
            eligible = matching or eligible
        if len(eligible) == 1:
            return eligible[0]
        if len(eligible) > 1:
            raise FunctionRouteError(
                f"Function {function_id} has multiple eligible models; select one explicitly"
            )
        raise FunctionRouteError(f"No enabled verified provider for function {function_id}")


def deployment_plan_path(*, repo_root: Path | None = None) -> Path:
    configured = os.getenv("DOTORI_DEPLOYMENT_PLAN_PATH", "").strip()
    if configured:
        return Path(configured)
    root = repo_root or Path(__file__).resolve().parents[2]
    return root / "data" / "config" / "deployment_scopes" / "production" / "deployment_plan.json"


def _string_tuple(payload: dict, field: str) -> tuple[str, ...]:
    value = payload.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DeploymentSnapshotError(f"{field} must be a list of strings")
    if len(value) != len(set(value)):
        raise DeploymentSnapshotError(f"{field} must not contain duplicates")
    return tuple(value)


def _mapping(payload: dict, field: str) -> dict[str, dict]:
    value = payload.get(field)
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, dict)
        for key, item in value.items()
    ):
        raise DeploymentSnapshotError(f"{field} must be an object of objects")
    return value


def _function_provider_map(payload: dict) -> dict[str, list[dict]]:
    value = payload.get("function_providers", {})
    if not isinstance(value, dict):
        raise DeploymentSnapshotError("function_providers must be an object")
    result: dict[str, list[dict]] = {}
    for function_id, candidates in value.items():
        if not isinstance(function_id, str) or not function_id:
            raise DeploymentSnapshotError("function_providers keys must be non-empty strings")
        if not isinstance(candidates, list) or not all(
            isinstance(candidate, dict) for candidate in candidates
        ):
            raise DeploymentSnapshotError(
                f"function_providers.{function_id} must be a list of objects"
            )
        result[function_id] = candidates
    return result


def _string_map(payload: dict, field: str) -> dict[str, str]:
    value = payload.get(field, {})
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    ):
        raise DeploymentSnapshotError(f"{field} must be an object of strings")
    return value


def _parse_snapshot(path: Path) -> DeploymentSnapshot:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DeploymentSnapshotError(f"Deployment plan does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DeploymentSnapshotError(f"Unable to read deployment plan {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DeploymentSnapshotError("Deployment plan must be a JSON object")
    schema_version = payload.get("schema_version")
    if schema_version not in SUPPORTED_DEPLOYMENT_PLAN_SCHEMA_VERSIONS:
        raise DeploymentSnapshotError(
            f"Unsupported deployment plan schema_version: {schema_version!r}"
        )

    selected_modules = _string_tuple(payload, "selected_modules")
    installed_capabilities = _string_tuple(payload, "installed_capabilities")
    enabled_capabilities = _string_tuple(payload, "enabled_capabilities")
    if not set(enabled_capabilities).issubset(installed_capabilities):
        raise DeploymentSnapshotError(
            "enabled_capabilities must be a subset of installed_capabilities"
        )
    capability_config = build_capability_config(enabled_capabilities)
    if payload.get("capability_revision") != capability_config.revision:
        raise DeploymentSnapshotError(
            "Deployment capability revision does not match enabled_capabilities"
        )
    generation_id = payload.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise DeploymentSnapshotError("generation_id is required")

    role_bindings = (
        _mapping(payload, "role_bindings")
        if schema_version >= 3
        else _mapping(payload, "provider_bindings")
    )
    if schema_version >= 3 and "provider_bindings" in payload:
        if _mapping(payload, "provider_bindings") != role_bindings:
            raise DeploymentSnapshotError(
                "provider_bindings compatibility alias must match role_bindings"
            )
    installed_models = _mapping(payload, "installed_models") if schema_version >= 3 else {}
    installed_providers = _mapping(payload, "installed_providers") if schema_version >= 4 else {}
    try:
        for provider_id, provider in installed_providers.items():
            if provider_id in installed_models:
                raise ValueError("Model and provider installation IDs must be disjoint")
            validate_provider_registration(provider_id, provider)
    except ValueError as exc:
        raise DeploymentSnapshotError(str(exc)) from exc
    function_providers = _function_provider_map(payload) if schema_version >= 3 else {}
    default_routes = _string_map(payload, "default_routes") if schema_version >= 3 else {}
    embedding_models = [
        model_id for model_id, model in installed_models.items()
        if model.get("role") == "embedding"
    ]
    if len(embedding_models) > 1:
        raise DeploymentSnapshotError("Only one server embedding model may be installed")
    for model_id, model in installed_models.items():
        if not isinstance(model.get("role"), str) or not model.get("role"):
            raise DeploymentSnapshotError(f"installed_models.{model_id}.role is required")
        for field in ("catalog_id", "artifact_revision", "runtime_ref"):
            if not isinstance(model.get(field), str) or not model.get(field):
                raise DeploymentSnapshotError(f"installed_models.{model_id}.{field} is required")
    for role, binding in role_bindings.items():
        if not isinstance(binding.get("enabled"), bool):
            raise DeploymentSnapshotError(f"role_bindings.{role}.enabled must be boolean")
        selected_model = binding.get("model_installation_id")
        if selected_model is not None and selected_model not in installed_models:
            raise DeploymentSnapshotError(
                f"role_bindings.{role}.model_installation_id references an unknown installed model"
            )
        if selected_model is not None and installed_models[selected_model].get("role") != role:
            raise DeploymentSnapshotError(
                f"role_bindings.{role}.model_installation_id has a mismatched role"
            )
    for role, binding in role_bindings.items():
        provider_id = binding.get("provider_installation_id")
        if provider_id is not None:
            provider = installed_providers.get(provider_id)
            if (binding.get("model_installation_id") is not None or provider is None
                    or provider.get("role") != role or binding.get("provider_id") != provider.get("provider_id")):
                raise DeploymentSnapshotError("Role binding references an incompatible provider installation")
    for function_id, candidates in function_providers.items():
        if schema_version >= 4:
            try:
                get_function(function_id)
            except ValueError as exc:
                raise DeploymentSnapshotError(str(exc)) from exc
        seen_models = set()
        for candidate in candidates:
            if schema_version >= 4:
                try:
                    model_id = candidate_identity(candidate)
                    validate_function_contract(function_id, candidate)
                except ValueError as exc:
                    raise DeploymentSnapshotError(str(exc)) from exc
            else:
                model_id = candidate.get("model_installation_id")
            references = installed_providers if candidate.get("provider_installation_id") else installed_models
            if candidate.get("request_evidence_scope") is not None:
                from capabilities.function_scope import validate_request_scope
                try:
                    validate_request_scope(candidate["request_evidence_scope"])
                    if (function_id != "text.generate" or candidate["request_evidence_scope"]["evidence_ref"] != candidate.get("evidence_ref", candidate.get("prior_evidence_ref"))):
                        raise ValueError("Request scope evidence reference mismatch")
                except ValueError as exc:
                    raise DeploymentSnapshotError(str(exc)) from exc
            grade = candidate.get("verification")
            if candidate.get("provider_installation_id"):
                if (grade != "declared_unverified"
                        or candidate.get("declaration_ref") != "dotori-parser-http-v1"
                        or candidate.get("evidence_ref")
                        or role_bindings.get(str(candidate.get("role")), {}).get("provider_installation_id") != model_id
                        or candidate.get("adapter_contract") != references.get(model_id, {}).get("adapter_contract")):
                    raise DeploymentSnapshotError("Model-free provider candidate has incompatible binding, adapter or verification")
            if model_id not in references:
                raise DeploymentSnapshotError(
                    f"function_providers.{function_id} references an unknown installed model"
                )
            if candidate.get("role") != references[model_id].get("role"):
                raise DeploymentSnapshotError(
                    f"function_providers.{function_id} provider role does not match its model"
                )
            if model_id in seen_models:
                raise DeploymentSnapshotError(
                    f"function_providers.{function_id} contains a duplicate model candidate"
                )
            seen_models.add(model_id)
            if grade not in {"declared_unverified", "contract_verified", "quality_verified"}:
                raise DeploymentSnapshotError(
                    f"function_providers.{function_id} has an invalid verification grade"
                )
            if grade == "declared_unverified" and not candidate.get("declaration_ref"):
                raise DeploymentSnapshotError(
                    f"function_providers.{function_id} requires a declaration_ref"
                )
            if grade in {"contract_verified", "quality_verified"} and not candidate.get("evidence_ref"):
                raise DeploymentSnapshotError(
                    f"function_providers.{function_id} requires an evidence_ref"
                )
    for function_id, model_id in default_routes.items():
        candidates = function_providers.get(function_id, [])
        if not any(
            (candidate.get("model_installation_id") or candidate.get("provider_installation_id")) == model_id
            and (
                candidate.get("verification") == "quality_verified"
                or (
                    function_id in {"text.generate", "text.embed"}
                    and candidate.get("verification") == "contract_verified"
                )
            )
            and role_bindings.get(str(candidate.get("role") or ""), {}).get("enabled")
            for candidate in candidates
        ):
            raise DeploymentSnapshotError(
                f"default_routes.{function_id} must select a verified provider candidate"
            )

    return DeploymentSnapshot(
        schema_version=schema_version,
        generation_id=generation_id,
        capability_revision=capability_config.revision,
        selected_modules=selected_modules,
        installed_capabilities=installed_capabilities,
        enabled_capabilities=enabled_capabilities,
        role_bindings=role_bindings,
        execution_roles=_mapping(payload, "execution_roles"),
        services=_string_tuple(payload, "services"),
        queues=_string_tuple(payload, "queues"),
        work_routes=_mapping(payload, "work_routes"),
        installed_models=installed_models,
        function_providers=function_providers,
        default_routes=default_routes,
        installed_providers=installed_providers,
    )


@lru_cache(maxsize=4)
def _load_cached(path: str) -> DeploymentSnapshot:
    return _parse_snapshot(Path(path))


def load_deployment_snapshot(*, path: Path | None = None) -> DeploymentSnapshot:
    target = path or deployment_plan_path()
    return _load_cached(str(target.resolve()))


def clear_deployment_snapshot_cache() -> None:
    _load_cached.cache_clear()
