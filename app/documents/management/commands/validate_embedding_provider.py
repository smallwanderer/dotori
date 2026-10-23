from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from document_ai.embedding.validation import (
    get_active_embedding_config,
    validate_active_embedding_provider,
)


class Command(BaseCommand):
    help = "Validate the active embedding provider, model dimension, sparse output, and pgvector schema."

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-db-schema",
            action="store_true",
            help="Only validate the provider runtime output; skip pgvector column dimension validation.",
        )
        parser.add_argument(
            "--config-only",
            action="store_true",
            help="Validate embedding provider registration and configuration without loading the model.",
        )

    def handle(self, *args, **options):
        try:
            if options["config_only"]:
                result = get_active_embedding_config()
            else:
                result = validate_active_embedding_provider(
                    validate_db_schema=not options["skip_db_schema"],
                )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Embedding provider validation passed."))
        for key, value in result.items():
            self.stdout.write(f"{key}: {value}")
