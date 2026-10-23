"""Validate and fingerprint capability selections inside a deployment plan."""

import hashlib
import json
from dataclasses import dataclass

from capabilities.registry import DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH, resolve_capabilities

CAPABILITY_CONFIG_SCHEMA_VERSION = 1
LEGACY_DOCUMENT_CAPABILITIES = (DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH)


class CapabilityConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CapabilityConfig:
    schema_version: int
    enabled_capabilities: tuple[str, ...]
    revision: str
    source: str

    def is_enabled(self, capability_id: str) -> bool:
        return capability_id in self.enabled_capabilities

    def as_dict(self) -> dict:
        return {"schema_version": self.schema_version, "enabled_capabilities": list(self.enabled_capabilities), "revision": self.revision}


def _revision(enabled_capabilities: tuple[str, ...]) -> str:
    canonical = json.dumps({"schema_version": CAPABILITY_CONFIG_SCHEMA_VERSION, "enabled_capabilities": list(enabled_capabilities)}, sort_keys=True, separators=(",", ":"))
    return f"cap-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"


def build_capability_config(enabled_capabilities: list[str] | tuple[str, ...], *, source: str = "explicit") -> CapabilityConfig:
    try:
        resolved = resolve_capabilities(enabled_capabilities)
    except ValueError as exc:
        raise CapabilityConfigError(str(exc)) from exc
    return CapabilityConfig(CAPABILITY_CONFIG_SCHEMA_VERSION, resolved, _revision(resolved), source)
