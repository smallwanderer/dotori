from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    dimension: int = Field(gt=0)
    model_input_max_tokens: int = Field(gt=0)
    languages: list[str] = Field(default_factory=list)


class EmbeddingProfileEntry(BaseModel):
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
    presets: list[Literal["speed", "balanced", "quality"]] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_supported_presets(self) -> "EmbeddingProfileEntry":
        if self.availability != "supported" and self.presets:
            raise ValueError("Only supported profiles may be assigned to presets.")
        if len(self.presets) != len(set(self.presets)):
            raise ValueError("presets must not contain duplicates.")
        return self


class EmbeddingCatalogEntry(BaseModel):
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
