from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from document_ai.models import EmbeddingGeneration
from document_ai.services.embedding_runtime_config import load_embedding_runtime
from llm_installation.embedding_config_store import (
    commit_active_embedding_runtime,
    get_embedding_runtime_config_path,
)


class Command(BaseCommand):
    help = "Roll back the active embedding runtime to a preserved generation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--scope",
            choices=("production", "development"),
            default="production",
        )
        parser.add_argument("--generation-id")

    def handle(self, *args, **options):
        scope = options["scope"]
        current = load_embedding_runtime(scope=scope)
        candidates = EmbeddingGeneration.objects.filter(
            scope=scope,
            status__in=("RETIRED", "READY"),
        ).exclude(generation_id=current.generation_id)
        if options.get("generation_id"):
            candidates = candidates.filter(
                generation_id=options["generation_id"]
            )
        target = candidates.order_by("-activated_at", "-updated_at").first()
        if target is None:
            raise CommandError("No rollback embedding generation is available.")

        active_path = get_embedding_runtime_config_path(scope)
        target_path = (
            active_path.parent
            / "embedding_generations"
            / target.generation_id
            / "runtime.json"
        )
        if not target_path.exists():
            raise CommandError(
                f"Rollback runtime snapshot is missing: {target_path}"
            )

        # Validate before changing the pointer.
        load_embedding_runtime(path=target_path, scope=scope)
        commit_active_embedding_runtime(scope, target.generation_id)
        with transaction.atomic():
            EmbeddingGeneration.objects.filter(
                generation_id=current.generation_id
            ).update(status="FAILED")
            target.status = "ACTIVE"
            target.activated_at = timezone.now()
            target.save(
                update_fields=["status", "activated_at", "updated_at"]
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Rolled back embedding runtime to {target.generation_id}. "
                "Restart app, embedding-worker, and search-worker."
            )
        )
