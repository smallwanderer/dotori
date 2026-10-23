from __future__ import annotations

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from accounts.api_responses import session_access_error
from documents.services.server_policy import build_server_policy_payload


@require_GET
def server_policy(request):
    access_error = session_access_error(request.user)
    if access_error is not None:
        return access_error

    server_policy_payload = build_server_policy_payload()
    return JsonResponse(
        {
            "ok": True,
            "operation_mode": server_policy_payload["operation_mode"],
            "policy": {
                "search_strategy": "hybrid",
                "search_top_k": settings.RAG_SEARCH_TOP_K,
                "retrieval_threshold": settings.RAG_RETRIEVAL_THRESHOLD,
            },
            "rag": server_policy_payload["rag"],
            "embedding": server_policy_payload["embedding"],
            "capabilities": server_policy_payload["capabilities"],
        }
    )
