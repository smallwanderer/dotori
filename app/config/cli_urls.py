from django.urls import path

from document_ai.cli_views import (
    CLIRAGStreamView,
    CLIVectorSearchView,
    cli_capabilities,
    cli_identity,
    cli_results,
    cli_result_detail,
    cli_source_results,
    cli_status,
)
from files.cli_views import cli_document_list, cli_upload


app_name = "cli_api"

urlpatterns = [
    path("identity/", cli_identity, name="identity"),
    path("status/", cli_status, name="status"),
    path("capabilities/", cli_capabilities, name="capabilities"),
    path("documents/", cli_document_list, name="documents"),
    path("documents/<uuid:source_uid>/results/", cli_source_results, name="document-results"),
    path("results/", cli_results, name="results"),
    path("results/<path:result_id>/", cli_result_detail, name="result-detail"),
    path("upload/", cli_upload, name="upload"),
    path("search/", CLIVectorSearchView.as_view(), name="search"),
    path("ask/stream/", CLIRAGStreamView.as_view(), name="ask-stream"),
]
