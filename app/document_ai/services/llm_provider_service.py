import os
from dataclasses import dataclass

from document_ai.models import LLMProvider, UserLLMPreference


def _normalize_llm_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3].rstrip("/")
    return normalized


def get_server_rag_base_url() -> str:
    return _normalize_llm_base_url(
        os.getenv("RAG_LLM_URL", os.getenv("TEXT2SQL_LLM_URL", "http://llm-parser:8080"))
    )


def get_server_rag_default_model() -> str:
    return os.getenv("RAG_LLM_MODEL", "google/gemma-4-E4B-it")


def get_server_rag_model_choices() -> list[str]:
    raw = os.getenv("RAG_AVAILABLE_MODELS", "")
    choices = [item.strip() for item in raw.split(",") if item.strip()]
    default_model = get_server_rag_default_model()
    if default_model and default_model not in choices:
        choices.insert(0, default_model)
    return choices


def upsert_llm_provider(
    *,
    owner,
    name: str,
    provider_type: str,
    base_url: str,
    default_model: str,
    api_key: str = "",
) -> tuple[LLMProvider | None, bool]:
    name = (name or "").strip()
    base_url = _normalize_llm_base_url(base_url)
    default_model = (default_model or "").strip()
    api_key = (api_key or "").strip()
    provider_type = provider_type or LLMProvider.PROVIDER_OPENAI_COMPATIBLE

    if not name or not base_url or not default_model:
        return None, False

    return LLMProvider.objects.update_or_create(
        owner=owner,
        name=name,
        defaults={
            "provider_type": provider_type,
            "base_url": base_url,
            "default_model": default_model,
            "api_key": api_key,
            "is_active": True,
        },
    )


def delete_llm_provider(*, owner, provider_id: str | None) -> int:
    if not provider_id:
        return 0
    deleted_count, _ = LLMProvider.objects.filter(id=provider_id, owner=owner).delete()
    return deleted_count


def get_or_create_llm_preference(user) -> UserLLMPreference:
    preference, _ = UserLLMPreference.objects.get_or_create(user=user)
    return preference


def set_user_rag_model(*, user, provider_id: str | None, rag_model: str) -> UserLLMPreference:
    preference = get_or_create_llm_preference(user)
    provider = None
    if provider_id:
        provider = LLMProvider.objects.filter(
            id=provider_id,
            owner=user,
            is_active=True,
        ).first()

    preference.rag_provider = provider
    preference.rag_model = (rag_model or "").strip()
    preference.save(update_fields=["rag_provider", "rag_model", "updated_at"])
    return preference


def get_user_llm_settings_context(user) -> dict:
    preference = get_or_create_llm_preference(user)
    return {
        "llm_providers": user.llm_providers.all().order_by("name"),
        "llm_preference": preference,
        "llm_provider_types": LLMProvider.PROVIDER_TYPE_CHOICES,
        "server_rag_base_url": get_server_rag_base_url(),
        "server_rag_default_model": get_server_rag_default_model(),
        "server_rag_model_choices": get_server_rag_model_choices(),
        "effective_rag_target": get_effective_rag_target(preference),
    }


def build_rag_llm_snapshot(user) -> dict:
    try:
        preference = UserLLMPreference.objects.select_related("rag_provider").get(user=user)
    except UserLLMPreference.DoesNotExist:
        return {}

    provider = preference.rag_provider
    if not provider or not provider.is_active:
        if preference.rag_model:
            return {
                "llm_provider_name": "Server default",
                "llm_base_url": get_server_rag_base_url(),
                "llm_model": preference.rag_model,
            }
        return {}

    return {
        "llm_provider": provider,
        "llm_provider_name": provider.name,
        "llm_base_url": provider.normalized_base_url,
        "llm_model": preference.get_rag_model(),
    }


def get_effective_rag_target(preference: UserLLMPreference) -> dict:
    provider = preference.rag_provider
    if provider and provider.is_active:
        model = preference.get_rag_model()
        return {
            "source": "external",
            "label": provider.name,
            "base_url": provider.normalized_base_url,
            "model": model,
            "provider_type": provider.get_provider_type_display(),
        }

    model = preference.rag_model or get_server_rag_default_model()
    return {
        "source": "server",
        "label": "Server default",
        "base_url": get_server_rag_base_url(),
        "model": model,
        "provider_type": "Server runtime",
    }


@dataclass(frozen=True)
class RAGLLMRequestConfig:
    base_url: str
    model: str
    headers: dict[str, str]

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/v1/chat/completions"


def resolve_rag_llm_request_config(rag_job) -> RAGLLMRequestConfig:
    base_url = _normalize_llm_base_url(
        rag_job.llm_base_url
        or os.getenv("RAG_LLM_URL", os.getenv("TEXT2SQL_LLM_URL", "http://llm-parser:8080"))
    )
    model = rag_job.llm_model or os.getenv("RAG_LLM_MODEL", "google/gemma-4-E4B-it")
    headers = {}
    provider = getattr(rag_job, "llm_provider", None)
    if rag_job.llm_provider_id and provider and provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    return RAGLLMRequestConfig(base_url=base_url, model=model, headers=headers)
