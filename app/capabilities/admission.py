"""Capability admission shared by all async producers and consumers."""

from dataclasses import dataclass

from capabilities.deployment import DeploymentSnapshotError, load_deployment_snapshot


@dataclass
class CapabilityAdmissionError(RuntimeError):
    capability_id: str
    code: str
    message: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


def capability_enabled(capability_id: str) -> bool:
    try:
        return load_deployment_snapshot().is_enabled(capability_id)
    except DeploymentSnapshotError:
        return False


def require_capability(capability_id: str):
    try:
        snapshot = load_deployment_snapshot()
    except DeploymentSnapshotError as exc:
        raise CapabilityAdmissionError(
            capability_id, "CAPABILITY_CONFIG_INVALID", f"Deployment configuration is invalid: {exc}"
        ) from exc
    if not snapshot.is_installed(capability_id):
        raise CapabilityAdmissionError(
            capability_id, "CAPABILITY_NOT_INSTALLED", f"Capability {capability_id} is not installed in the active deployment"
        )
    if not snapshot.is_enabled(capability_id):
        raise CapabilityAdmissionError(
            capability_id, "CAPABILITY_DISABLED", f"Capability {capability_id} is disabled on this server"
        )
    return snapshot
