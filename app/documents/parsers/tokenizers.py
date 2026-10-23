"""Tokenizer loading with explicit identity and an explicit fallback policy."""
from dataclasses import dataclass, fields
import logging

from django.conf import settings

logger = logging.getLogger(__name__)


@dataclass
class LoadedTokenizer:
    tokenizer: object
    requested_id: str
    requested_revision: str | None
    loaded_id: str
    loaded_revision: str | None
    observed_revision: str | None
    fallback_used: bool = False
    fallback_reason: str | None = None

    def identity(self):
        return {field.name: getattr(self, field.name) for field in fields(self) if field.name != "tokenizer"}


def normalized_revision(revision):
    return revision if revision not in {"", "legacy", "main", "external", None} else None


def load_runtime_tokenizer(runtime, *, allow_fallback=False):
    from transformers import AutoTokenizer

    requested_id = runtime.tokenizer_id
    revision = normalized_revision(runtime.tokenizer_revision)
    kwargs = {"revision": revision}
    options = getattr(runtime, "tokenizer_options", {})
    if hasattr(options, "model_dump"):
        options = options.model_dump(exclude_unset=True)
    if options:
        kwargs.update(options)
    fallback_reason = None
    loaded_id, loaded_revision = requested_id, revision
    try:
        tokenizer = AutoTokenizer.from_pretrained(requested_id, **kwargs)
    except Exception as exc:
        if not allow_fallback:
            raise RuntimeError(f"Cannot load candidate tokenizer {requested_id}@{revision}: {exc}") from exc
        loaded_id = getattr(settings, "PARSER_TOKENIZER_ID", "BAAI/bge-m3")
        loaded_revision = normalized_revision(getattr(settings, "PARSER_TOKENIZER_REVISION", "5617a9f"))
        fallback_reason = str(exc)
        logger.warning("Tokenizer %s@%s failed; using %s@%s: %s", requested_id, revision,
                       loaded_id, loaded_revision, exc)
        # Candidate custom-code permissions do not apply to a different fallback.
        tokenizer = AutoTokenizer.from_pretrained(loaded_id, revision=loaded_revision)
    return LoadedTokenizer(tokenizer, requested_id, revision, loaded_id, loaded_revision,
                           getattr(tokenizer, "init_kwargs", {}).get("_commit_hash"),
                           fallback_reason is not None, fallback_reason)


def wrap_chunk_tokenizer(loaded, *, max_tokens):
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
    if max_tokens <= 0:
        raise ValueError("Chunk token budget must be positive")
    return HuggingFaceTokenizer(tokenizer=loaded.tokenizer, max_tokens=max_tokens)
