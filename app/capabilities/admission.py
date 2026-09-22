"""Capability admission shared by all async producers and consumers."""

import json
from dataclasses import dataclass

from capabilities.config import CapabilityConfigError, capability_config_path, load_capability_config


@dataclass(frozen=True)
class CapabilityAdmissionError(RuntimeError):
    capability_id: str
    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


def capability_enabled(capability_id: str) -> bool:
    try:
        return load_capability_config().is_enabled(capability_id)
    except CapabilityConfigError:
        return False


def require_capability(capability_id: str):
    try:
        config = load_capability_config()
    except CapabilityConfigError as exc:
        raise CapabilityAdmissionError(
            capability_id, "CAPABILITY_CONFIG_INVALID", f"Capability configuration is invalid: {exc}"
        ) from exc
    if not config.is_enabled(capability_id):
        raise CapabilityAdmissionError(
            capability_id, "CAPABILITY_DISABLED", f"Capability {capability_id} is disabled on this server"
        )
    if config.source != "legacy_missing_config":
        deployment_path = capability_config_path().parent / "deployment_scopes" / "production" / "deployment_plan.json"
        try:
            deployment = json.loads(deployment_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CapabilityAdmissionError(
                capability_id, "CAPABILITY_NOT_INSTALLED", f"Capability {capability_id} has no valid deployment plan"
            ) from exc
        deployed_capabilities = deployment.get("enabled_capabilities")
        if not isinstance(deployed_capabilities, list) or capability_id not in deployed_capabilities:
            raise CapabilityAdmissionError(
                capability_id, "CAPABILITY_NOT_INSTALLED", f"Capability {capability_id} is not installed in the active deployment"
            )
        if deployment.get("capability_revision") != config.revision:
            raise CapabilityAdmissionError(
                capability_id, "CAPABILITY_UNAVAILABLE", f"Capability {capability_id} configuration and deployment revisions differ",
                retryable=True,
            )
    return config
