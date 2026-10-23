"""Static function contracts. IDs describe data contracts, never executable imports."""
from dataclasses import dataclass
from types import MappingProxyType
from capabilities.registry import DOCUMENT_SEMANTIC_SEARCH


@dataclass(frozen=True)
class FunctionDefinition:
    function_id: str
    contract_version: int
    input_schema: str
    output_schema: str
    preferred_role: str
    adapters: tuple[str, ...]

    def metadata(self):
        return {key: getattr(self, key) for key in
                ("contract_version", "input_schema", "output_schema", "preferred_role")}


FUNCTION_REGISTRY = MappingProxyType({
    "document.extract": FunctionDefinition("document.extract", 1, "dotori.document.source.v1",
        "dotori.document.extract.v1", "parser", ("dotori-parser-http-v1",)),
    "text.embed": FunctionDefinition("text.embed", 1, "dotori.text.embedding-input.v1",
        "dotori.text.embedding-output.v1", "embedding", ("dotori-internal-embed-v1",)),
    "text.generate": FunctionDefinition("text.generate", 1, "dotori.chat.messages.v1",
        "dotori.chat.completion.v1", "llm", ("openai-compatible-chat-completions-v1",)),
})


@dataclass(frozen=True)
class CompositionDefinition:
    function_id: str
    contract_version: int
    input_schema: str
    output_schema: str
    preferred_role: str
    adapter_contract: str
    primitive_function_id: str
    work_kind: str
    capability_id: str
    plan_adapter_path: str
    intent_validator_path: str
    delivery_validator_path: str


# Static contract definitions, not a second provider/routing registry. Keeping
# them separate prevents installation planners advertising domain compositions
# as model-native functions. No durable work route is enabled by this mapping.
COMPOSITION_DEFINITIONS = MappingProxyType({
    "document.embed": CompositionDefinition("document.embed", 1,
        "dotori.document.extract.v1", "dotori.document.embedding-result.v1",
        "embedding", "dotori-document-embedding-v1", "text.embed", "embed_document",
        DOCUMENT_SEMANTIC_SEARCH, "documents.processing.embedding_plan_adapter.EMBEDDING_DOMAIN_ADAPTER",
        "documents.processing.embedding_contract.parse_embedding_intent",
        "documents.processing.embedding_contract.parse_embedding_delivery"),
})


def validate_composition_provider(function_id, *, snapshot, adapter_contract):
    """Validate the selected primitive provider, not the domain schema.

    Work/capability/deployment generation and runtime/domain checks remain
    mandatory at the owning admission boundary. This port alone admits no work.
    """
    definition = COMPOSITION_DEFINITIONS.get(function_id)
    if definition is None:
        raise ValueError(f"Unknown composition: {function_id}")
    if adapter_contract != definition.adapter_contract:
        raise ValueError("Incompatible composition adapter")
    primitive = get_function(definition.primitive_function_id)
    if primitive.preferred_role != definition.preferred_role:
        raise ValueError("Composition primitive role mismatch")
    binding = snapshot.role_bindings.get(definition.preferred_role, {})
    selection = {"explicit_provider_installation_id": binding.get("provider_installation_id")}
    if binding.get("model_installation_id"):
        selection["explicit_model_installation_id"] = binding["model_installation_id"]
    provider = snapshot.resolve_function_provider(definition.primitive_function_id, **selection)
    validate_function_contract(definition.primitive_function_id, provider)
    if provider.get("role") != definition.preferred_role:
        raise ValueError("Composition provider role mismatch")
    return provider


def get_function(function_id):
    try:
        return FUNCTION_REGISTRY[function_id]
    except KeyError as exc:
        raise ValueError(f"Unknown function contract: {function_id}") from exc


def candidate_identity(candidate):
    model = candidate.get("model_installation_id")
    provider = candidate.get("provider_installation_id")
    if (model is not None and (not isinstance(model, str) or not model)
            or provider is not None and (not isinstance(provider, str) or not provider)):
        raise ValueError("Function installation reference must be a non-empty string")
    if bool(model) == bool(provider):
        raise ValueError("Function candidate must reference exactly one model or provider installation")
    return model or provider


def validate_function_contract(function_id, candidate):
    definition = get_function(function_id)
    if type(candidate.get("contract_version")) is not int:
        raise ValueError("Function contract_version must be an integer")
    for key, expected in definition.metadata().items():
        if candidate.get(key) != expected:
            raise ValueError(f"Function {function_id} has incompatible {key}")
    if candidate.get("adapter_contract") not in definition.adapters:
        raise ValueError(f"Function {function_id} has incompatible adapter_contract")


def validate_provider_registration(provider_id, provider):
    # Only the shipped parser adapter is registered here; not a plugin loader.
    if (provider_id != "parser:docling:1" or provider != {
        "role": "parser", "provider_id": "docling", "registration_revision": "1",
        "adapter_contract": "dotori-parser-http-v1", "config_source": "DOCUMENT_PARSER_BACKEND",
    }):
        raise ValueError("Unknown or incompatible model-free provider registration")
