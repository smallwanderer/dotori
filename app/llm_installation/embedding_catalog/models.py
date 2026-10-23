from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from llm_installation.embedding_catalog.provider_options import TokenizerOptions, validate_provider_options

from llm_installation.function_catalog import FunctionCapabilitySpec


class EmbeddingFootprintSpec(BaseModel):
    """Declared memory cost of holding this model resident.

    Weights are derived from the parameter count at claim time rather than
    stored per device, because the same model costs fp16 on CUDA and fp32 on
    CPU -- the same reason the LLM planner derives weights from
    ``parameter_count_b`` instead of storing a single number.

    ``peak_activation_mb`` is 0 until a real run measures it. It is recorded
    separately from the weights so a reservation can say which half is a
    declared floor and which half has been observed.
    """

    model_config = ConfigDict(extra="forbid")

    parameter_count_m: float = Field(gt=0)
    peak_activation_mb: int = Field(default=0, ge=0)


class EmbeddingModelEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    description: str = ""
    license: str | None = None
    repo_id: str = Field(min_length=1)
    revision: str = Field(min_length=7)
    tokenizer_id: str = Field(min_length=1)
    tokenizer_revision: str = Field(min_length=7)
    tokenizer_options: TokenizerOptions = Field(default_factory=TokenizerOptions)
    dimension: int = Field(gt=0)
    model_input_max_tokens: int = Field(gt=0)
    languages: list[str] = Field(default_factory=list)
    footprint: EmbeddingFootprintSpec | None = None


class _ProviderOptionsMixin(BaseModel):
    provider_options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_options(self):
        self.provider_options = validate_provider_options(
            self.provider, self.provider_options, dimension=self.dimension,
            query_prefix=self.query_prefix, document_prefix=self.document_prefix,
        )
        if self.provider in {"sentence_transformers", "openai_compatible"} and self.supports_sparse:
            raise ValueError(f"Provider {self.provider} does not produce sparse embeddings")
        if self.provider == "bgem3_hybrid" and not self.normalize_embeddings:
            raise ValueError("bgem3_hybrid requires normalized embeddings")
        return self


class EmbeddingProfileEntry(_ProviderOptionsMixin):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    store: str = Field(min_length=1)
    dimension: int = Field(gt=0)
    supports_sparse: bool
    normalize_embeddings: bool
    distance_strategy: Literal["inner_product", "cosine", "l2"]
    query_prefix: str = ""
    document_prefix: str = ""
    availability: Literal["supported", "experimental", "unavailable"]
    priority: int = 0
    functions: list[FunctionCapabilitySpec] = Field(default_factory=list)


class EmbeddingCatalogEntry(_ProviderOptionsMixin):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    description: str = ""
    license: str | None = None
    model_id: str
    repo_id: str
    revision: str
    tokenizer_id: str
    tokenizer_revision: str
    tokenizer_options: TokenizerOptions = Field(default_factory=TokenizerOptions)
    provider: str
    store: str
    dimension: int
    model_input_max_tokens: int
    supports_sparse: bool
    normalize_embeddings: bool
    distance_strategy: Literal["inner_product", "cosine", "l2"]
    query_prefix: str = ""
    document_prefix: str = ""
    availability: Literal["supported", "experimental", "unavailable"]
    priority: int = 0
    presets: list[Literal["speed", "balanced", "quality"]] = Field(
        default_factory=list
    )
    languages: list[str] = Field(default_factory=list)
    footprint: EmbeddingFootprintSpec | None = None
    functions: list[FunctionCapabilitySpec] = Field(default_factory=list)
