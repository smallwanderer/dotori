import os
from dataclasses import dataclass

from django.utils import timezone

from document_ai.models import LLMEndpoint, UserLLMPreference


def _normalize_llm_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3].rstrip("/")
    return normalized


def get_server_rag_base_url() -> str:
    return _normalize_llm_base_url(
        os.getenv("RAG_LLM_URL", "http://llama-rag:8080")
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


def upsert_llm_endpoint(
    *,
    owner,
    name: str,
    endpoint_type: str,
    base_url: str,
    default_model: str,
    api_key: str = "",
) -> tuple[LLMEndpoint | None, bool]:
    name = (name or "").strip()
    base_url = _normalize_llm_base_url(base_url)
    default_model = (default_model or "").strip()
    api_key = (api_key or "").strip()
    endpoint_type = endpoint_type or LLMEndpoint.ENDPOINT_OPENAI_COMPATIBLE

    if not name or not base_url or not default_model:
        return None, False

    return LLMEndpoint.objects.update_or_create(
        owner=owner,
        name=name,
        defaults={
            "endpoint_type": endpoint_type,
            "base_url": base_url,
            "default_model": default_model,
            "api_key": api_key,
            "is_active": True,
        },
    )


def delete_llm_endpoint(*, owner, endpoint_id: str | None) -> int:
    if not endpoint_id:
        return 0
    deleted_count, _ = LLMEndpoint.objects.filter(id=endpoint_id, owner=owner).delete()
    return deleted_count


def check_llm_endpoint(*, owner, endpoint_id: str | None, timeout: int = 8) -> LLMEndpoint | None:
    if not endpoint_id:
        return None

    endpoint = LLMEndpoint.objects.filter(id=endpoint_id, owner=owner).first()
    if endpoint is None:
        return None

    import requests

    headers = {}
    if endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"

    models_url = f"{endpoint.normalized_base_url}/v1/models"
    status = "failed"
    message = ""
    try:
        response = requests.get(models_url, headers=headers, timeout=timeout)
        if 200 <= response.status_code < 300:
            payload = response.json()
            models = payload.get("data", []) if isinstance(payload, dict) else []
            model_ids = {
                item.get("id")
                for item in models
                if isinstance(item, dict) and item.get("id")
            }
            status = "ok"
            if endpoint.default_model in model_ids:
                message = f"연결됨. 기본 모델 '{endpoint.default_model}' 확인됨."
            elif model_ids:
                message = (
                    f"연결됨. {len(model_ids)}개 모델을 확인했지만 "
                    f"'{endpoint.default_model}'은 목록에 없습니다."
                )
            else:
                message = "연결됨. 모델 목록 응답을 받았습니다."
        else:
            message = f"HTTP {response.status_code}: {response.text[:240]}"
    except requests.RequestException as exc:
        message = str(exc)[:240]
    except ValueError:
        message = "모델 목록 응답을 JSON으로 해석할 수 없습니다."

    endpoint.last_checked_at = timezone.now()
    endpoint.last_check_status = status
    endpoint.last_check_message = message[:500]
    endpoint.save(
        update_fields=[
            "last_checked_at",
            "last_check_status",
            "last_check_message",
            "updated_at",
        ]
    )
    return endpoint


def get_or_create_llm_preference(user) -> UserLLMPreference:
    preference, _ = UserLLMPreference.objects.get_or_create(user=user)
    return preference


def set_user_rag_model(*, user, endpoint_id: str | None, rag_model: str) -> UserLLMPreference:
    preference = get_or_create_llm_preference(user)
    endpoint = None
    if endpoint_id:
        endpoint = LLMEndpoint.objects.filter(
            id=endpoint_id,
            owner=user,
            is_active=True,
        ).first()

    preference.rag_endpoint = endpoint
    preference.rag_model = (rag_model or "").strip()
    preference.save(update_fields=["rag_endpoint", "rag_model", "updated_at"])
    return preference


def get_user_llm_settings_context(user) -> dict:
    preference = get_or_create_llm_preference(user)
    return {
        "llm_endpoints": user.llm_endpoints.all().order_by("name"),
        "llm_preference": preference,
        "llm_endpoint_types": LLMEndpoint.ENDPOINT_TYPE_CHOICES,
        "server_rag_base_url": get_server_rag_base_url(),
        "server_rag_default_model": get_server_rag_default_model(),
        "server_rag_model_choices": get_server_rag_model_choices(),
        "effective_rag_target": get_effective_rag_target(preference),
    }


def build_rag_llm_snapshot(user) -> dict:
    try:
        preference = UserLLMPreference.objects.select_related("rag_endpoint").get(user=user)
    except UserLLMPreference.DoesNotExist:
        return {}

    endpoint = preference.rag_endpoint
    if not endpoint or not endpoint.is_active:
        if preference.rag_model:
            return {
                "llm_endpoint_name": "Server default",
                "llm_base_url": get_server_rag_base_url(),
                "llm_model": preference.rag_model,
            }
        return {}

    return {
        "llm_endpoint": endpoint,
        "llm_endpoint_name": endpoint.name,
        "llm_base_url": endpoint.normalized_base_url,
        "llm_model": preference.get_rag_model(),
    }


def get_effective_rag_target(preference: UserLLMPreference) -> dict:
    endpoint = preference.rag_endpoint
    if endpoint and endpoint.is_active:
        model = preference.get_rag_model()
        return {
            "source": "external",
            "label": endpoint.name,
            "base_url": endpoint.normalized_base_url,
            "model": model,
            "endpoint_type": endpoint.get_endpoint_type_display(),
        }

    model = preference.rag_model or get_server_rag_default_model()
    return {
        "source": "server",
        "label": "Server default",
        "base_url": get_server_rag_base_url(),
        "model": model,
        "endpoint_type": "Server runtime",
    }


@dataclass(frozen=True)
class RAGLLMRequestConfig:
    base_url: str
    model: str
    headers: dict[str, str]

    @property
    def responses_url(self) -> str:
        return f"{self.base_url}/v1/responses"

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/v1/chat/completions"


def resolve_rag_llm_request_config(rag_job) -> RAGLLMRequestConfig:
    base_url = _normalize_llm_base_url(
        rag_job.llm_base_url
        or os.getenv("RAG_LLM_URL", "http://llama-rag:8080")
    )
    model = rag_job.llm_model or os.getenv("RAG_LLM_MODEL", "google/gemma-4-E4B-it")
    headers = {}
    endpoint = getattr(rag_job, "llm_endpoint", None)
    if rag_job.llm_endpoint_id and endpoint and endpoint.api_key:
        headers["Authorization"] = f"Bearer {endpoint.api_key}"
    return RAGLLMRequestConfig(base_url=base_url, model=model, headers=headers)
