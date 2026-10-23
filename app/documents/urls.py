from django.urls import include, path
from documents.search.views import (
    RAGStreamView,
    SandboxPageView,
    VectorSearchView,
    VectorSandboxView,
)
from documents.status_views import server_policy
from documents.conversation_views import (
    ConversationCancelView,
    ConversationDetailView,
    ConversationListView,
    ConversationMessageListView,
)
from documents.operation_views import (
    collect_operation_resources,
    operation_events,
    operation_metrics,
    operation_resources,
    operation_status,
    operation_trace,
)

app_name = "documents"

# Grouped by feature, not by owning package: config.urls mounts each group
# under api/v1/<feature>/ so moving a view to another app does not move its URL.
search_urlpatterns = [
    path("", VectorSearchView.as_view(), name="vector-search"),
    path("tuning/", VectorSandboxView.as_view(), name="vector-tuning"),
    path("sandbox/", SandboxPageView.as_view(), name="sandbox-page"),
]

rag_urlpatterns = [
    path("stream/", RAGStreamView.as_view(), name="rag-stream"),
    path("conversations/", ConversationListView.as_view(), name="rag-conversations"),
    path("conversations/<uuid:uid>/", ConversationDetailView.as_view(), name="rag-conversation"),
    path("conversations/<uuid:uid>/messages/", ConversationMessageListView.as_view(), name="rag-conversation-messages"),
    path("conversations/<uuid:uid>/cancel/", ConversationCancelView.as_view(), name="rag-conversation-cancel"),
    path("conversations/<uuid:conversation_uid>/messages/stream/", RAGStreamView.as_view(), name="rag-conversation-stream"),
]

server_policy_urlpatterns = [
    path("", server_policy, name="server-policy"),
]

operations_urlpatterns = [
    path("status/", operation_status, name="operation-status"),
    path("metrics/", operation_metrics, name="operation-metrics"),
    path("events/", operation_events, name="operation-events"),
    path("traces/<str:trace_id>/", operation_trace, name="operation-trace"),
    path("resources/", operation_resources, name="operation-resources"),
    path("resources/collect/", collect_operation_resources, name="collect-operation-resources"),
]

urlpatterns = [
    path("search/", include(search_urlpatterns)),
    path("rag/", include(rag_urlpatterns)),
    path("server-policy/", include(server_policy_urlpatterns)),
    path("operations/", include(operations_urlpatterns)),
    # Internal embedding endpoints (livez/readyz/embed) live only in
    # config.embedding_urls, served only by embedding-executor's model-owning
    # process. They are deliberately absent from this urlconf.
]

# DEPRECATED -- scheduled for removal.
#
# The old /api/document-ai/v1/... layout, kept only so clients that have not
# moved to /api/v1/... keep working. Every first-party caller (web SPA, nginx
# template, scripts, docs) already uses the new paths, so nothing in this
# repository depends on this list.
#
# Do not add routes here, and do not reuse the "document_ai" namespace in new
# code; use "documents:<name>" instead. Remove this list, its mount in
# config.urls, and its regression test once deployed installations have been
# upgraded.
deprecated_document_ai_urlpatterns = [
    path("v1/search/", VectorSearchView.as_view(), name="vector-search"),
    path("v1/rag/stream/", RAGStreamView.as_view(), name="rag-stream"),
    path("v1/rag/conversations/", ConversationListView.as_view(), name="rag-conversations"),
    path("v1/rag/conversations/<uuid:uid>/", ConversationDetailView.as_view(), name="rag-conversation"),
    path("v1/rag/conversations/<uuid:uid>/messages/", ConversationMessageListView.as_view(), name="rag-conversation-messages"),
    path("v1/rag/conversations/<uuid:uid>/cancel/", ConversationCancelView.as_view(), name="rag-conversation-cancel"),
    path("v1/rag/conversations/<uuid:conversation_uid>/messages/stream/", RAGStreamView.as_view(), name="rag-conversation-stream"),
    path("v1/server-policy/", server_policy, name="server-policy"),
    path("v1/operations/status/", operation_status, name="operation-status"),
    path("v1/operations/metrics/", operation_metrics, name="operation-metrics"),
    path("v1/operations/events/", operation_events, name="operation-events"),
    path("v1/operations/traces/<str:trace_id>/", operation_trace, name="operation-trace"),
    path("v1/operations/resources/", operation_resources, name="operation-resources"),
    path("v1/operations/resources/collect/", collect_operation_resources, name="collect-operation-resources"),
    path("v1/tuning/", VectorSandboxView.as_view(), name="vector-tuning"),
    path("sandbox/", SandboxPageView.as_view(), name="sandbox-page"),
]
