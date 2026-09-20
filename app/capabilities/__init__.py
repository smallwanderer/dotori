"""Server-wide capability declarations and activation state."""

from capabilities.config import CapabilityConfig, CapabilityConfigError, load_capability_config, write_capability_config
from capabilities.registry import DOCUMENT_EXTRACT, DOCUMENT_SEMANTIC_SEARCH, CapabilityDefinition, get_capability, resolve_capabilities

__all__ = ["CapabilityConfig", "CapabilityConfigError", "CapabilityDefinition", "DOCUMENT_EXTRACT", "DOCUMENT_SEMANTIC_SEARCH", "get_capability", "load_capability_config", "resolve_capabilities", "write_capability_config"]
