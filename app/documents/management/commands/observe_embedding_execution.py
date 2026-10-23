"""Collect an authenticated observation from the model-owning executor."""
import json
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from capabilities.execution import executor_url
from documents.embedding.routing import runtime_identity
from documents.services.embedding_runtime_config import get_active_embedding_runtime
from llm_installation.function_applicability import ObservedFunctionExecution


class Command(BaseCommand):
    help = "Read the active embedding executor's execution observation without changing deployment."

    def add_arguments(self, parser):
        parser.add_argument("--result-output")
        parser.add_argument("--timeout", type=float, default=120)

    def handle(self, *args, **options):
        token = settings.EMBEDDING_INTERNAL_TOKEN
        if not token or options["timeout"] <= 0:
            raise CommandError("An internal token and positive timeout are required")
        runtime = get_active_embedding_runtime()
        try:
            response = requests.get(f"{executor_url('embedding')}/execution-observation/",
                headers={"Authorization": f"Bearer {token}"}, timeout=options["timeout"])
            response.raise_for_status()
            payload = response.json()
            if payload.get("execution_identity") != runtime_identity(runtime):
                raise ValueError("executor identity mismatch")
            observed = ObservedFunctionExecution.model_validate(payload["observation"])
            if (observed.runtime_generation_id != runtime.generation_id
                    or observed.target_id != runtime.catalog_id or observed.model_revision != runtime.model_revision
                    or observed.backend_id != runtime.provider):
                raise ValueError("observation identity mismatch")
            result = {f"embedding:{runtime.generation_id}": {"text.embed": observed.model_dump(mode="json")}}
            serialized = json.dumps(result, ensure_ascii=False, indent=2)
            if options["result_output"]:
                with Path(options["result_output"]).open("x", encoding="utf-8") as output:
                    output.write(serialized + "\n")
        except (OSError, ValueError, KeyError, requests.RequestException) as exc:
            raise CommandError("Unable to collect the active executor observation") from exc
        self.stdout.write(serialized)
