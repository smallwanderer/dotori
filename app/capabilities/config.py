"""Read and write the server-wide capability activation artifact."""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

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


def capability_config_path(*, repo_root: Path | None = None) -> Path:
    configured = os.getenv("DOTORI_CAPABILITIES_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured)
    root = repo_root or Path(__file__).resolve().parents[2]
    return root / "data" / "config" / "capabilities.json"


def _revision(enabled_capabilities: tuple[str, ...]) -> str:
    canonical = json.dumps({"schema_version": CAPABILITY_CONFIG_SCHEMA_VERSION, "enabled_capabilities": list(enabled_capabilities)}, sort_keys=True, separators=(",", ":"))
    return f"cap-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"


def build_capability_config(enabled_capabilities: list[str] | tuple[str, ...], *, source: str = "explicit") -> CapabilityConfig:
    try:
        resolved = resolve_capabilities(enabled_capabilities)
    except ValueError as exc:
        raise CapabilityConfigError(str(exc)) from exc
    return CapabilityConfig(CAPABILITY_CONFIG_SCHEMA_VERSION, resolved, _revision(resolved), source)


def load_capability_config(*, path: Path | None = None, repo_root: Path | None = None) -> CapabilityConfig:
    config_path = path or capability_config_path(repo_root=repo_root)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return build_capability_config(LEGACY_DOCUMENT_CAPABILITIES, source="legacy_missing_config")
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityConfigError(f"Unable to read capability config {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CapabilityConfigError("Capability config must be a JSON object")
    if payload.get("schema_version") != CAPABILITY_CONFIG_SCHEMA_VERSION:
        raise CapabilityConfigError(f"Unsupported capability config schema_version: {payload.get('schema_version')!r}")
    enabled = payload.get("enabled_capabilities")
    if not isinstance(enabled, list) or not all(isinstance(item, str) for item in enabled):
        raise CapabilityConfigError("enabled_capabilities must be a list of strings")
    config = build_capability_config(enabled)
    persisted_revision = payload.get("revision")
    if persisted_revision and persisted_revision != config.revision:
        raise CapabilityConfigError("Capability config revision does not match its contents")
    return config


def write_capability_config(enabled_capabilities: list[str] | tuple[str, ...], *, path: Path | None = None, repo_root: Path | None = None) -> CapabilityConfig:
    config = build_capability_config(enabled_capabilities)
    config_path = path or capability_config_path(repo_root=repo_root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = config_path.with_suffix(f"{config_path.suffix}.tmp")
    temporary.write_text(json.dumps(config.as_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, config_path)
    return config
