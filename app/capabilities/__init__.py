"""Server-wide capability declarations and deployment state."""

from capabilities.deployment import DeploymentSnapshot, DeploymentSnapshotError, load_deployment_snapshot
from capabilities.registry import DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH, CapabilityDefinition, get_capability, resolve_capabilities

__all__ = ["CapabilityDefinition", "DeploymentSnapshot", "DeploymentSnapshotError", "DOCUMENT_EXTRACT", "DOCUMENT_SEMANTIC_SEARCH", "get_capability", "load_deployment_snapshot", "resolve_capabilities"]
