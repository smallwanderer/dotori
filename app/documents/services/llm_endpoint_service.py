import os
from dataclasses import dataclass

from django.utils import timezone

from llm_installation.rag_runtime_config import get_cached_server_rag_target
from documents.models import LLMEndpoint, UserLLMPreference


_PROVIDER_RESOLUTION_SNAPSHOT_KEY = "_provider_resolution"


def _normalize_llm_base_url(base_url: str) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[:-3].rstrip("/")
    return normalized


def get_server_rag_base_url() -> str:
    return _normalize_llm_base_url(
        os.getenv("RAG_LLM_URL", get_cached_server_rag_target().base_url)
    )


def get_server_rag_default_model() -> str:
    return get_cached_server_rag_target().model


def get_server_rag_model_choices() -> list[str]:
    default_model = get_server_rag_default_model()
    return [default_model] if default_model else []


def upsert_llm_endpoint(
    *,
    workspace,
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
        workspace=workspace,
        name=name,
        defaults={
            "owner": owner,
            "endpoint_type": endpoint_type,
            "base_url": base_url,
            "default_model": default_model,
            "api_key": api_key,
            "is_active": True,
        },
    )


def delete_llm_endpoint(*, workspace, endpoint_id: str | None) -> int:
    if not endpoint_id:
        return 0
    deleted_count, _ = LLMEndpoint.objects.filter(id=endpoint_id, workspace=workspace).delete()
    return deleted_count


def check_llm_endpoint(*, workspace, endpoint_id: str | None, timeout: int = 8) -> LLMEndpoint | None:
    if not endpoint_id:
        return None

    endpoint = LLMEndpoint.objects.filter(id=endpoint_id, workspace=workspace).first()
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


def get_or_create_llm_preference(workspace, *, owner=None) -> UserLLMPreference:
    preference, _ = UserLLMPreference.objects.get_or_create(
        workspace=workspace, defaults={"user": owner}
    )
    return preference


def set_user_rag_model(*, workspace, owner=None, endpoint_id: str | None, rag_model: str) -> UserLLMPreference:
    preference = get_or_create_llm_preference(workspace, owner=owner)
    endpoint = None
    if endpoint_id:
        endpoint = LLMEndpoint.objects.filter(
            id=endpoint_id,
            workspace=workspace,
            is_active=True,
        ).first()

    preference.rag_endpoint = endpoint
    preference.rag_model = (rag_model or "").strip()
    preference.save(update_fields=["rag_endpoint", "rag_model", "updated_at"])
    return preference


def get_user_llm_settings_context(workspace, *, owner=None) -> dict:
    preference = get_or_create_llm_preference(workspace, owner=owner)
    return {
        "llm_endpoints": workspace.llm_endpoints.all().order_by("name"),
        "llm_preference": preference,
        "llm_endpoint_types": LLMEndpoint.ENDPOINT_TYPE_CHOICES,
        "server_rag_base_url": get_server_rag_base_url(),
        "server_rag_default_model": get_server_rag_default_model(),
        "server_rag_model_choices": get_server_rag_model_choices(),
        "effective_rag_target": get_effective_rag_target(preference),
    }


def build_rag_llm_snapshot(workspace) -> dict:
    try:
        preference = UserLLMPreference.objects.select_related("rag_endpoint").get(workspace=workspace)
    except UserLLMPreference.DoesNotExist:
        target, provider_resolution = _resolve_server_text_generate_route()
        return {
            **target.as_snapshot(),
            _PROVIDER_RESOLUTION_SNAPSHOT_KEY: provider_resolution,
        }

    endpoint = preference.rag_endpoint
    if endpoint and endpoint.is_active:
        model = preference.get_rag_model()
        provider_resolution = {
            "function_id": "text.generate",
            "source": "workspace_endpoint",
            "selection": "explicit_workspace_endpoint",
            "verification": "operator_declared_unverified",
            "endpoint_id": str(endpoint.pk),
            "endpoint_type": str(endpoint.endpoint_type),
            "model": model,
        }
        return {
            "llm_endpoint": endpoint,
            "llm_endpoint_name": endpoint.name,
            "llm_base_url": endpoint.normalized_base_url,
            "llm_model": model,
            _PROVIDER_RESOLUTION_SNAPSHOT_KEY: provider_resolution,
        }

    target, provider_resolution = _resolve_server_text_generate_route()
    selected_model = (preference.rag_model or target.model).strip()
    if selected_model != target.model:
        raise LLMRuntimeNotConfigured(
            "The selected server model is not the active deployment's registered text.generate provider."
        )
    return {
        **target.as_snapshot(),
        _PROVIDER_RESOLUTION_SNAPSHOT_KEY: provider_resolution,
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
    provider_resolution: dict[str, str]

    @property
    def responses_url(self) -> str:
        return f"{self.base_url}/v1/responses"

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/v1/chat/completions"


def split_rag_llm_snapshot(snapshot: dict | None) -> tuple[dict, dict]:
    """Separate persisted RAGJob columns from its immutable routing evidence."""
    values = dict(snapshot or {})
    provider_resolution = values.pop(_PROVIDER_RESOLUTION_SNAPSHOT_KEY, None)
    metrics = (
        {"provider_resolution": dict(provider_resolution)}
        if isinstance(provider_resolution, dict)
        else {}
    )
    return values, metrics


def _resolve_server_text_generate_route(
    *,
    expected_deployment_generation_id: str | None = None,
    expected_model_installation_id: str | None = None,
    expected_runtime_generation_id: str | None = None,
):
    from capabilities.deployment import load_deployment_snapshot
    from llm_installation.rag_runtime_config import (
        LLMRuntimeNotConfigured,
        get_cached_server_rag_target,
        load_llm_runtime_config,
    )

    try:
        deployment = load_deployment_snapshot()
        if (
            expected_deployment_generation_id
            and deployment.generation_id != expected_deployment_generation_id
        ):
            raise ValueError("The deployment plan changed after this RAG turn was admitted.")
        runtime_config = load_llm_runtime_config()
        runtime_target = runtime_config.get("target")
        if not isinstance(runtime_target, dict):
            raise ValueError("The active server LLM runtime target is missing.")
        runtime_generation_id = str(runtime_target.get("generation_id") or "").strip()
        runtime_model = str(runtime_target.get("model") or "").strip()
        if not runtime_generation_id or not runtime_model:
            raise ValueError("The active server LLM runtime identity is incomplete.")
        if (
            expected_runtime_generation_id
            and runtime_generation_id != expected_runtime_generation_id
        ):
            raise ValueError("The active server LLM runtime changed after this RAG turn was admitted.")

        installation_id = f"llm:{runtime_generation_id}"
        if (
            expected_model_installation_id
            and installation_id != expected_model_installation_id
        ):
            raise ValueError("The admitted LLM installation is no longer active.")
        candidate = deployment.resolve_function_provider(
            "text.generate",
            explicit_model_installation_id=expected_model_installation_id,
        ) if expected_model_installation_id else deployment.resolve_function_provider("text.generate")
        if candidate.get("role") != "llm":
            raise ValueError("The selected text.generate provider is not the active LLM role.")
        if candidate.get("model_installation_id") != installation_id:
            raise ValueError("The selected text.generate provider does not match the active LLM runtime.")
        installed_model = deployment.installed_models.get(installation_id)
        if not isinstance(installed_model, dict) or installed_model.get("catalog_id") != runtime_model:
            raise ValueError("The deployment model identity does not match the active LLM runtime.")
        target = get_cached_server_rag_target()
        configured_base_url = _normalize_llm_base_url(
            str(runtime_target.get("base_url") or "")
        )
        if (
            target.model != runtime_model
            or _normalize_llm_base_url(target.base_url) != configured_base_url
        ):
            raise ValueError("The cached server LLM target differs from its active runtime snapshot.")
    except LLMRuntimeNotConfigured:
        raise
    except (OSError, ValueError) as exc:
        raise LLMRuntimeNotConfigured(
            f"No usable deployment route is registered for text.generate: {exc}"
        ) from exc

    provider_resolution = {
        "function_id": "text.generate",
        "source": "server_runtime",
        "selection": "deployment_default_route",
        "verification": candidate.get("verification", ""),
        "model_installation_id": installation_id,
        "model": runtime_model,
        "deployment_generation_id": deployment.generation_id,
        "runtime_generation_id": runtime_generation_id,
    }
    for key in ("evidence_ref", "backend_id"):
        value = candidate.get(key)
        if value:
            provider_resolution[key] = str(value)
    return target, provider_resolution


def _resolve_legacy_server_rag_target(rag_job):
    """Keep already-created pre-routing RAG jobs recoverable during rollout."""
    from capabilities.execution import require_registered_role
    from llm_installation.rag_runtime_config import LLMRuntimeNotConfigured

    try:
        require_registered_role("llm")
    except ValueError as exc:
        raise LLMRuntimeNotConfigured(str(exc)) from exc

    server_target = None
    if not getattr(rag_job, "llm_base_url", "") or not getattr(rag_job, "llm_model", ""):
        server_target = get_cached_server_rag_target()
    server_base_url = server_target.base_url if server_target else ""
    server_model = server_target.model if server_target else ""
    base_url = _normalize_llm_base_url(
        getattr(rag_job, "llm_base_url", "")
        or server_base_url
        or os.getenv("RAG_LLM_URL", "http://rag-runtime:8080")
    )
    model = (
        getattr(rag_job, "llm_model", "")
        or server_model
        or get_server_rag_default_model()
    )
    provider_resolution = {
        "function_id": "text.generate",
        "source": "server_runtime",
        "selection": "pre_routing_job_compatibility",
        "verification": "catalog_route_not_applied",
        "model": model,
    }
    try:
        from capabilities.deployment import load_deployment_snapshot

        provider_resolution["deployment_generation_id"] = (
            load_deployment_snapshot().generation_id
        )
    except (OSError, ValueError):
        pass
    if not base_url or not model:
        raise LLMRuntimeNotConfigured("The server LLM target is incomplete.")
    return base_url, model, provider_resolution


def resolve_rag_llm_request_config(rag_job) -> RAGLLMRequestConfig:
    from llm_installation.rag_runtime_config import LLMRuntimeNotConfigured

    headers = {}
    endpoint = getattr(rag_job, "llm_endpoint", None)
    metrics = getattr(rag_job, "performance_metrics", None)
    metrics = metrics if isinstance(metrics, dict) else {}
    admitted_resolution = metrics.get("provider_resolution")
    admitted_resolution = admitted_resolution if isinstance(admitted_resolution, dict) else {}

    if rag_job.llm_endpoint_id:
        if endpoint is None or not endpoint.is_active:
            raise LLMRuntimeNotConfigured(
                "The explicitly selected workspace LLM endpoint is no longer available."
            )
        if admitted_resolution.get("source") == "workspace_endpoint" and str(
            admitted_resolution.get("endpoint_id") or ""
        ) != str(rag_job.llm_endpoint_id):
            raise LLMRuntimeNotConfigured(
                "The workspace endpoint differs from the endpoint selected for this RAG turn."
            )
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"
        base_url = _normalize_llm_base_url(rag_job.llm_base_url or endpoint.normalized_base_url)
        model = rag_job.llm_model or endpoint.default_model
        provider_resolution = {
            "function_id": "text.generate",
            "source": "workspace_endpoint",
            "selection": "explicit_workspace_endpoint",
            "verification": "operator_declared_unverified",
            "endpoint_id": str(rag_job.llm_endpoint_id),
            "endpoint_type": str(getattr(endpoint, "endpoint_type", "unknown")),
            "model": model,
        }
        endpoint_name = getattr(endpoint, "name", "")
        if endpoint_name:
            provider_resolution["endpoint_name"] = str(endpoint_name)
    else:
        if admitted_resolution.get("source") == "workspace_endpoint":
            raise LLMRuntimeNotConfigured(
                "The workspace endpoint selected for this RAG turn is no longer attached."
            )
        if admitted_resolution.get("source") not in (None, "server_runtime"):
            raise LLMRuntimeNotConfigured(
                "The stored provider source is invalid for a server-local RAG turn."
            )
        if admitted_resolution.get("selection") == "deployment_default_route":
            if not all(
                admitted_resolution.get(field)
                for field in (
                    "deployment_generation_id",
                    "runtime_generation_id",
                    "model_installation_id",
                )
            ):
                raise LLMRuntimeNotConfigured(
                    "The admitted server route snapshot is incomplete."
                )
            target, provider_resolution = _resolve_server_text_generate_route(
                expected_deployment_generation_id=str(
                    admitted_resolution["deployment_generation_id"]
                ),
                expected_model_installation_id=str(
                    admitted_resolution["model_installation_id"]
                ),
                expected_runtime_generation_id=str(
                    admitted_resolution["runtime_generation_id"]
                ),
            )
            base_url = _normalize_llm_base_url(target.base_url)
            model = target.model
        elif admitted_resolution.get("selection") in (
            None,
            "server_runtime_target",
            "pre_routing_job_compatibility",
        ):
            base_url, model, provider_resolution = _resolve_legacy_server_rag_target(
                rag_job
            )
        else:
            raise LLMRuntimeNotConfigured(
                "The stored server route selection is not supported."
            )
        job_base_url = _normalize_llm_base_url(getattr(rag_job, "llm_base_url", ""))
        job_model = str(getattr(rag_job, "llm_model", "") or "").strip()
        if job_base_url and job_base_url != base_url:
            raise LLMRuntimeNotConfigured(
                "The server LLM endpoint changed after this RAG turn was admitted."
            )
        if job_model and job_model != model:
            raise LLMRuntimeNotConfigured(
                "The server LLM model differs from the deployment route for this RAG turn."
            )
    return RAGLLMRequestConfig(
        base_url=base_url,
        model=model,
        headers=headers,
        provider_resolution=provider_resolution,
    )
