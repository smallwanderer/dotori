from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class HfSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_id: str
    revision: str = "main"
    gated: bool = False
    trust_remote_code: bool = False


class ArtifactSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["gguf", "safetensors", "awq", "gptq", "compress-tensors"]
    filename: str | None = None
    quant: str
    dtype: str | None = None

    download_size_mb: float | None = Field(default=None, gt=0)
    disk_required_mb: int = 0


class ModelMetadataSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    architecture: str | None = None

    parameter_count_b: float = Field(gt=0)
    active_parameter_count_b: float | None = None
    is_moe: bool = False

    max_context_length: int = 4096

    hidden_size: int
    num_hidden_layers: int
    num_attention_heads: int
    num_key_value_heads: int | None = None
    head_dim: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def compute_num_key_value_heads(self) -> "ModelMetadataSpec":
        if self.num_key_value_heads is None or self.num_key_value_heads == self.num_attention_heads:
            self.num_key_value_heads = self.num_attention_heads
        return self

    @model_validator(mode="after")
    def compute_head_dim(self) -> "ModelMetadataSpec":
        if not self.head_dim and self.hidden_size and self.num_attention_heads:
            self.head_dim = self.hidden_size // self.num_attention_heads
        return self


class CapabilitySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modalities: list[Literal["text", "vision", "audio", "embedding"]] = Field(
        default_factory=lambda: ["text"]
    )
    tasks: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)

class PolicySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible: bool = True
    allow_auto_select: bool = True
    allow_manual_select: bool = True
    safety: Literal["safe", "danger"] = "safe"
    tags: list[str] = Field(default_factory=list)


class ModelCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    display_name: str
    description: str = ""
    license: str | None = None
    model_metadata: ModelMetadataSpec
    capabilities: CapabilitySpec = Field(default_factory=CapabilitySpec)


class ArtifactCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    model_id: str
    enabled: bool = True
    priority: int = 0
    display_name: str | None = None
    description: str = ""
    hf: HfSpec
    artifact: ArtifactSpec
    backend_profiles: list[
        Literal["vllm-cuda", "llamacpp-cpu", "llamacpp-gpu-offload"]
    ] = Field(min_length=1)
    policy: PolicySpec = Field(default_factory=PolicySpec)

    @model_validator(mode="after")
    def validate_backend_profiles(self) -> "ArtifactCatalogEntry":
        allowed_by_format = {
            "gguf": {"llamacpp-cpu", "llamacpp-gpu-offload"},
            "safetensors": {"vllm-cuda"},
            "awq": {"vllm-cuda"},
            "gptq": {"vllm-cuda"},
            "compress-tensors": {"vllm-cuda"},
        }
        profiles = self.backend_profiles
        if len(profiles) != len(set(profiles)):
            raise ValueError("backend_profiles must not contain duplicates")
        invalid = set(profiles) - allowed_by_format[self.artifact.format]
        if invalid:
            raise ValueError(
                f"Backend profiles {sorted(invalid)} are incompatible with "
                f"artifact format {self.artifact.format}."
            )
        return self


class RAGModelCatalogEntry(BaseModel):
    """Resolved model-artifact candidate consumed by the planner."""

    id: str
    model_id: str
    enabled: bool = True
    priority: int = 0
    display_name: str
    description: str = ""
    license: str | None = None
    hf: HfSpec
    artifact: ArtifactSpec
    backend_profiles: list[
        Literal["vllm-cuda", "llamacpp-cpu", "llamacpp-gpu-offload"]
    ]
    model_metadata: ModelMetadataSpec
    capabilities: CapabilitySpec = Field(default_factory=CapabilitySpec)
    policy: PolicySpec = Field(default_factory=PolicySpec)

    @property
    def source(self) -> str:
        return "catalog/models+artifacts"

    @property
    def device_label(self) -> str:
        """Declared CPU/GPU capability for this artifact, independent of the
        current machine's hardware (e.g. GGUF artifacts that support both
        CPU-only and GPU-offload serving report "GPU/CPU")."""
        profiles = set(self.backend_profiles)
        has_cpu = "llamacpp-cpu" in profiles
        has_gpu = bool(profiles & {"llamacpp-gpu-offload", "vllm-cuda"})
        if has_cpu and has_gpu:
            return "GPU/CPU"
        return "GPU" if has_gpu else "CPU"


class RAGModelCatalogDocument(BaseModel):
    schema_version: int = 2
    catalog_name: str = "rag-serving-model-catalog"
    updated_at: str | None = None
    entries: list[RAGModelCatalogEntry] = Field(default_factory=list)
