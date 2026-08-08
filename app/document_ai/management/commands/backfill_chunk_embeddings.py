from django.core.management.base import BaseCommand

from config.enums import AIStatus
from document_ai.embedding.embeding_models import embed_document
from document_ai.embedding.store_registry import get_embedding_store_instance
from document_ai.models import DocumentChunk
from document_ai.parsers.config import get_embedding_backend, get_embedding_model
from document_ai.parsers.text_utils import normalize_extracted_text


class Command(BaseCommand):
    help = "Backfill chunk embeddings for a specific embedding backend."

    def add_arguments(self, parser):
        parser.add_argument(
            "--backend",
            default=get_embedding_backend(),
            help="Embedding backend name. Example: bgem3_hybrid",
        )
        parser.add_argument(
            "--model-name",
            default=get_embedding_model(),
            help="Embedding model name.",
        )
        parser.add_argument(
            "--user-email",
            help="Only process chunks owned by the given user email.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Optional maximum number of chunks to process.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Recompute embeddings even if the target backend already exists.",
        )

    def handle(self, *args, **options):
        backend = options["backend"]
        model_name = options["model_name"]
        store = get_embedding_store_instance(
            model_name=model_name,
            backend=backend,
        )

        chunk_qs = DocumentChunk.objects.select_related(
            "parse_result",
            "parse_result__node",
        ).filter(parse_result__status=AIStatus.COMPLETED)

        user_email = options.get("user_email")
        if user_email:
            chunk_qs = chunk_qs.filter(parse_result__node__owner__email=user_email)

        if not options["overwrite"]:
            existing_chunk_ids = store.completed_chunk_ids()
            chunk_qs = chunk_qs.exclude(id__in=existing_chunk_ids)
        limit = options.get("limit")
        if limit:
            chunk_qs = chunk_qs[:limit]

        chunks = list(chunk_qs)
        total = len(chunks)

        if total == 0:
            self.stdout.write(self.style.WARNING("No chunks to process."))
            return

        self.stdout.write(
            f"Embedding {total} chunks with backend={backend} model={model_name}"
        )

        success_count = 0
        failed_count = 0

        for index, chunk in enumerate(chunks, start=1):
            text = normalize_extracted_text(chunk.text or "")
            if not text:
                failed_count += 1
                self.stdout.write(
                    self.style.WARNING(f"[{index}/{total}] chunk_id={chunk.id} skipped: empty text")
                )
                continue

            try:
                embedding = embed_document(
                    text=text,
                    model_name=model_name,
                    backend=backend,
                )
                store.save_chunk_embedding(
                    chunk=chunk,
                    embedding=embedding,
                    status=AIStatus.COMPLETED,
                )
                success_count += 1
                self.stdout.write(f"[{index}/{total}] chunk_id={chunk.id} done")
            except Exception as exc:
                failed_count += 1
                store.mark_chunk_embedding_failed(
                    chunk=chunk,
                    error_message=str(exc)[:255],
                    status=AIStatus.FAILED,
                )
                self.stdout.write(
                    self.style.ERROR(f"[{index}/{total}] chunk_id={chunk.id} failed: {exc}")
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Completed backfill. success={success_count} failed={failed_count}"
            )
        )
