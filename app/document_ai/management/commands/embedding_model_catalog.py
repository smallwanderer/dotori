import json

from django.core.management.base import BaseCommand

from llm_installation.embedding_catalog import load_embedding_catalog


class Command(BaseCommand):
    help = "List checked-in embedding catalog entries and support status."

    def add_arguments(self, parser):
        parser.add_argument("--json", action="store_true", dest="as_json")
        parser.add_argument(
            "--all",
            action="store_true",
            help="Include experimental and unavailable entries.",
        )

    def handle(self, *args, **options):
        entries = load_embedding_catalog()
        if not options["all"]:
            entries = [
                entry
                for entry in entries
                if entry.availability == "supported"
            ]

        rows = [entry.model_dump(mode="json") for entry in entries]
        if options["as_json"]:
            self.stdout.write(
                json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True)
            )
            return

        for entry in entries:
            self.stdout.write(
                f"{entry.id}: model={entry.repo_id}@{entry.revision} "
                f"provider={entry.provider} store={entry.store} "
                f"dimension={entry.dimension} sparse={entry.supports_sparse} "
                f"availability={entry.availability}"
            )
