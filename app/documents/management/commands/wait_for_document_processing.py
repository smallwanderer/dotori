import time

from django.core.management.base import BaseCommand, CommandError

from document_ai.processing.drain import document_drain_snapshot


class Command(BaseCommand):
    help = "Wait until document queues and executor receipts remain empty."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=float, default=1800.0)
        parser.add_argument("--poll-interval", type=float, default=1.0)
        parser.add_argument("--stable-polls", type=int, default=2)

    def handle(self, *args, **options):
        timeout = max(0.0, options["timeout"])
        poll_interval = max(0.05, options["poll_interval"])
        stable_polls = max(1, options["stable_polls"])
        deadline = time.monotonic() + timeout
        consecutive_empty = 0

        while True:
            try:
                snapshot = document_drain_snapshot()
            except Exception as exc:
                raise CommandError(
                    f"Unable to inspect document processing state: {exc}"
                ) from exc

            if snapshot.pending_work == 0:
                consecutive_empty += 1
                if consecutive_empty >= stable_polls:
                    self.stdout.write("Document processing drained.")
                    return
            else:
                consecutive_empty = 0

            if time.monotonic() >= deadline:
                raise CommandError(
                    "Document processing did not drain before the deadline "
                    f"(active={snapshot.active_receipts}, "
                    f"parse={snapshot.parse_queue_depth}, "
                    f"embed={snapshot.embed_queue_depth})."
                )
            time.sleep(poll_interval)
