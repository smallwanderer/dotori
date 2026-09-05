from __future__ import annotations

import json
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from config.enums import AIStatus
from document_ai.embedding.registry import get_embedding_provider
from document_ai.embedding.store_registry import get_embedding_store_instance
from document_ai.models import (
    DocumentChunk,
    EmbeddingGeneration,
)
from document_ai.parsers.text_utils import normalize_extracted_text
from document_ai.services.embedding_runtime_config import load_embedding_runtime
from llm_installation.embedding_catalog import (
    get_embedding_catalog_entry_for_preset,
)
from llm_installation.embedding_config_store import (
    commit_active_embedding_runtime,
    get_embedding_runtime_config_path,
    write_embedding_runtime_generation,
)


LOCK_NAME = "dotori_embedding_runtime_change"


class Command(BaseCommand):
    help = (
        "Build and validate a candidate embedding generation. Activation is "
        "allowed only after complete corpus coverage."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--preset",
            choices=("speed", "balanced", "quality"),
            default="balanced",
        )
        parser.add_argument(
            "--scope",
            choices=("production",),
            default="production",
        )
        parser.add_argument(
            "--activate",
            action="store_true",
            help="Atomically activate the candidate after validation.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Diagnostic candidate limit. Limited runs cannot activate.",
        )
        parser.add_argument(
            "--force-reembed",
            action="store_true",
            help="Rebuild even when the selected catalog revision is active.",
        )

    def _acquire_lock(self) -> bool:
        if connection.vendor != "postgresql":
            return True
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s))",
                [LOCK_NAME],
            )
            return bool(cursor.fetchone()[0])

    def _release_lock(self) -> None:
        if connection.vendor != "postgresql":
            return
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))",
                [LOCK_NAME],
            )

    def handle(self, *args, **options):
        if options["activate"] and options.get("limit"):
            raise CommandError("A limited candidate run cannot be activated.")
        if not self._acquire_lock():
            raise CommandError("Another embedding runtime change is in progress.")

        try:
            self._change_runtime(**options)
        finally:
            self._release_lock()

    def _change_runtime(self, **options):
        entry = get_embedding_catalog_entry_for_preset(options["preset"])
        active_runtime = load_embedding_runtime(scope=options["scope"])
        if (
            not options.get("force_reembed")
            and active_runtime.catalog_id == entry.id
            and active_runtime.model_revision == entry.revision
        ):
            self.stdout.write(
                self.style.SUCCESS(
                    f"Embedding runtime is already active: {entry.id}@{entry.revision}"
                )
            )
            return
        # Preserve the previous active snapshot as a rollback artifact,
        # including legacy .env-derived installations.
        active_path = get_embedding_runtime_config_path(options["scope"])
        previous_generation_dir = (
            active_path.parent
            / "embedding_generations"
            / active_runtime.generation_id
        )
        previous_generation_path = previous_generation_dir / "runtime.json"
        if not previous_generation_path.exists():
            previous_generation_dir.mkdir(parents=True, exist_ok=True)
            previous_generation_path.write_text(
                json.dumps(
                    active_runtime.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        generation_id = (
            f"{options['scope']}-embedding-{entry.id}-"
            f"{entry.revision[:12]}-{int(time.time())}"
        )
        generation_dir = write_embedding_runtime_generation(
            scope=options["scope"],
            generation_id=generation_id,
            entry=entry,
        )
        runtime = load_embedding_runtime(
            path=generation_dir / "runtime.json",
            scope=options["scope"],
        )

        chunk_qs = (
            DocumentChunk.objects.select_related(
                "parse_result",
                "parse_result__node",
            )
            .filter(
                parse_result__status=AIStatus.COMPLETED,
                parse_result__node__trashed=False,
                parse_result__node__ai_processing_enabled=True,
            )
            .order_by("id")
        )
        expected_chunks = chunk_qs.count()
        if options.get("limit"):
            chunk_qs = chunk_qs[: options["limit"]]

        generation, _ = EmbeddingGeneration.objects.update_or_create(
            generation_id=generation_id,
            defaults={
                "scope": runtime.scope,
                "runtime_fingerprint": runtime.runtime_fingerprint,
                "catalog_id": runtime.catalog_id,
                "model_id": runtime.model_id,
                "model_revision": runtime.model_revision,
                "provider": runtime.provider,
                "store": runtime.store,
                "dimension": runtime.dimension,
                "supports_sparse": runtime.supports_sparse,
                "status": "PREPARING",
                "expected_chunks": expected_chunks,
                "completed_chunks": 0,
                "failed_chunks": 0,
            },
        )

        # provider proxies to dotori-document's /embed when run outside it (see
        # RemoteBGEM3Provider) -- correct either way, but running this inside
        # dotori-document avoids paying a per-chunk HTTP round trip for what
        # can be a large bulk re-embed.
        provider = get_embedding_provider(runtime=runtime)
        store = get_embedding_store_instance(runtime=runtime)
        try:
            probe = provider.embed_query(
                "dotori embedding runtime healthcheck",
                max_length=32,
            )
            store.validate_embedding(probe)
        except Exception as exc:
            generation.status = "FAILED"
            generation.failed_chunks = expected_chunks
            generation.save(
                update_fields=["status", "failed_chunks", "updated_at"]
            )
            raise CommandError(
                f"Candidate provider validation failed: {exc}"
            ) from exc

        generation.status = "EMBEDDING"
        generation.save(update_fields=["status", "updated_at"])
        completed = 0
        failed = 0
        for chunk in chunk_qs.iterator(chunk_size=100):
            text = normalize_extracted_text(chunk.text or "")
            if not text:
                failed += 1
                continue
            try:
                embedding = provider.embed_document(text)
                store.save_chunk_embedding(
                    chunk=chunk,
                    embedding=embedding,
                    status=AIStatus.COMPLETED,
                )
                completed += 1
            except Exception as exc:
                failed += 1
                store.mark_chunk_embedding_failed(
                    chunk=chunk,
                    error_message=str(exc)[:255],
                    status=AIStatus.FAILED,
                )

            if (completed + failed) % 100 == 0:
                EmbeddingGeneration.objects.filter(pk=generation.pk).update(
                    completed_chunks=completed,
                    failed_chunks=failed,
                )
                self.stdout.write(
                    f"candidate={generation_id} completed={completed} "
                    f"failed={failed}"
                )

        generation.completed_chunks = completed
        generation.failed_chunks = failed
        full_coverage = (
            options.get("limit") is None
            and failed == 0
            and completed == expected_chunks
        )
        generation.status = "READY" if full_coverage else "FAILED"
        generation.save(
            update_fields=[
                "completed_chunks",
                "failed_chunks",
                "status",
                "updated_at",
            ]
        )
        if not full_coverage:
            raise CommandError(
                "Candidate coverage validation failed: "
                f"expected={expected_chunks} completed={completed} failed={failed}"
            )

        if not options["activate"]:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Candidate generation is READY: {generation_id}"
                )
            )
            return

        commit_active_embedding_runtime(options["scope"], generation_id)
        with transaction.atomic():
            previous_generation, _ = EmbeddingGeneration.objects.get_or_create(
                generation_id=active_runtime.generation_id,
                defaults={
                    "scope": active_runtime.scope,
                    "runtime_fingerprint": active_runtime.runtime_fingerprint,
                    "catalog_id": active_runtime.catalog_id,
                    "model_id": active_runtime.model_id,
                    "model_revision": active_runtime.model_revision,
                    "provider": active_runtime.provider,
                    "store": active_runtime.store,
                    "dimension": active_runtime.dimension,
                    "supports_sparse": active_runtime.supports_sparse,
                    "status": "ACTIVE",
                },
            )
            if previous_generation.pk != generation.pk:
                previous_generation.status = "ACTIVE"
                previous_generation.save(update_fields=["status", "updated_at"])
            EmbeddingGeneration.objects.filter(
                scope=options["scope"],
                status="ACTIVE",
            ).exclude(pk=generation.pk).update(status="RETIRED")
            generation.status = "ACTIVE"
            generation.activated_at = timezone.now()
            generation.save(
                update_fields=["status", "activated_at", "updated_at"]
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Activated embedding generation: {generation_id}. "
                "Restart app and dotori-document before "
                "serving new requests."
            )
        )
