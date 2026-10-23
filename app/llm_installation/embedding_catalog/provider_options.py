"""Small, typed adapter settings shared by catalog and runtime schemas."""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class SentenceTransformersOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trust_remote_code: StrictBool = False
    truncate_dim: int | None = Field(default=None, gt=0, strict=True)
    query_task: str | None = Field(default=None, min_length=1)
    document_task: str | None = Field(default=None, min_length=1)
    query_prompt_name: str | None = Field(default=None, min_length=1)
    document_prompt_name: str | None = Field(default=None, min_length=1)


class OpenAICompatibleOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None retains the legacy text-embedding-3 behavior. Explicit False omits it.
    send_dimensions: StrictBool | None = None


def validate_provider_options(
    provider: str, options: dict[str, Any], *, dimension: int,
    query_prefix: str = "", document_prefix: str = "",
) -> dict[str, Any]:
    if provider == "sentence_transformers":
        parsed = SentenceTransformersOptions.model_validate(options)
        if parsed.truncate_dim is not None and parsed.truncate_dim != dimension:
            raise ValueError("truncate_dim must equal the profile output dimension")
        for kind, prefix in (("query", query_prefix), ("document", document_prefix)):
            if prefix and getattr(parsed, f"{kind}_prompt_name"):
                raise ValueError(f"{kind}_prefix and {kind}_prompt_name are mutually exclusive")
    elif provider == "openai_compatible":
        parsed = OpenAICompatibleOptions.model_validate(options)
    elif not options:
        return {}
    else:
        raise ValueError(f"Provider {provider} does not accept provider_options")
    # Preserve explicit nulls too: the stdlib-only host writer signs the raw
    # profile options, so normalizing them away would invalidate its hash.
    return parsed.model_dump(exclude_unset=True)


class TokenizerOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trust_remote_code: StrictBool = False
