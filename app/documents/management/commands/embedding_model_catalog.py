import json

from django.core.management.base import BaseCommand

from llm_installation.embedding_catalog import load_embedding_catalog
from llm_installation.embedding_footprint import (
    describe_embedding_fit,
    describe_embedding_footprint,
)
from llm_installation.runtime_probe import probe_server_runtime


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

        try:
            profile = probe_server_runtime()
        except Exception:
            profile = None

        for entry in entries:
            self.stdout.write(
                f"{entry.id}: model={entry.repo_id}@{entry.revision} "
                f"provider={entry.provider} store={entry.store} "
                f"dimension={entry.dimension} sparse={entry.supports_sparse} "
                f"availability={entry.availability}"
            )
            self.stdout.write(f"    memory: {describe_embedding_footprint(entry)}")
            if profile is not None:
                fit_line = describe_embedding_fit(entry, profile)
                style = (
                    self.style.ERROR if "NOFIT" in fit_line
                    else self.style.WARNING if "RISKY" in fit_line
                    else self.style.SUCCESS
                )
                self.stdout.write(f"    fit: {style(fit_line)}")
