"""Model-free execution role declarations shared by app and orchestrator.

Runtime files remain the source of model/provider settings.  These declarations
describe how an installed role is reached, without loading a parser or model.
"""

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ExecutionRole:
    role: str
    transport: str
    config_source: str
    compose_service: str | None = None
    endpoint_env: str | None = None
    default_endpoint: str | None = None
    receipt_repository_path: str | None = None


EXECUTION_ROLES = MappingProxyType({
    "parser": ExecutionRole("parser", "orchestrator_http", "DOCUMENT_PARSER_BACKEND", "parser-executor", "PARSER_EXECUTOR_URL", "http://parser-executor:8002", "documents.services.receipt_repository.REPOSITORY"),
    "embedding": ExecutionRole("embedding", "orchestrator_http_and_request_http", "embedding_runtime.json", "embedding-executor", "EMBEDDING_SERVICE_URL", "http://embedding-executor:8001", "documents.services.receipt_repository.REPOSITORY"),
    "llm": ExecutionRole("llm", "request_http", "llm_runtime.json"),
})


def get_execution_role(role: str) -> ExecutionRole:
    try:
        return EXECUTION_ROLES[role]
    except KeyError as exc:
        raise ValueError(f"Unknown execution role: {role}") from exc


def require_registered_role(role: str) -> ExecutionRole:
    """Check the active deployment registration before selecting a server role.

    Old deployments without execution_roles retain their existing behavior.
    Once the installer writes that field, the declaration is authoritative.
    """
    from capabilities.deployment import load_deployment_snapshot

    spec = get_execution_role(role)
    snapshot = load_deployment_snapshot()
    registrations = snapshot.execution_roles
    registered = registrations.get(role)
    expected = {
        "transport": spec.transport,
        "config_source": spec.config_source,
        "compose_service": spec.compose_service,
    }
    if registered != expected:
        raise ValueError(f"Execution role {role} is not registered in the active deployment")
    return spec


def executor_url(role: str) -> str:
    """Resolve a registered HTTP executor without guessing a fallback role."""
    import os

    spec = require_registered_role(role)
    if spec.endpoint_env is None or spec.default_endpoint is None:
        raise ValueError(f"Execution role {role} has no HTTP executor")
    value = os.getenv(spec.endpoint_env, spec.default_endpoint).strip().rstrip("/")
    if not value:
        raise ValueError(f"Execution role {role} has no configured endpoint")
    return value
