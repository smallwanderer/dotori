from __future__ import annotations

from dataclasses import replace

from django.core.management.base import BaseCommand, CommandError

from document_ai.llm_installation_helper.catalog import catalog_rows, get_catalog_entry
from document_ai.llm_installation_helper.cli import json_output, model_detail_dict, print_model_table
from document_ai.llm_installation_helper.runtime_probe import probe_server_runtime


class Command(BaseCommand):
    help = "List, search, or show built-in RAG LLM model catalog entries."

    def add_arguments(self, parser):
        parser.add_argument(
            "--cluster-mode",
            action="store_true",
            help="Preview runtime resolution for clustered/distributed serving.",
        )
        parser.add_argument(
            "action",
            choices=["list", "search", "show"],
            help="Catalog action to run.",
        )
        parser.add_argument(
            "query",
            nargs="?",
            default="",
            help="Search query or model id for show.",
        )
        parser.add_argument(
            "--json-output",
            action="store_true",
            help="Print machine-readable JSON.",
        )

    def handle(self, *args, **options):
        profile = probe_server_runtime()
        if options["cluster_mode"]:
            profile = replace(profile, cluster_mode=True)
        action = options["action"]
        query = options["query"]

        if action == "show":
            entry = get_catalog_entry(query)
            if entry is None:
                raise CommandError(f"Unknown catalog model id: {query}")
            rows = catalog_rows(profile)
            row = next(item for item in rows if item["id"] == entry.id)
            detail = model_detail_dict(row)
            if options["json_output"]:
                self.stdout.write(json_output(detail))
                return
            for key, value in detail.items():
                self.stdout.write(f"{key}: {value}")
            return

        rows = catalog_rows(
            profile,
            query=query if action == "search" else "",
        )
        if options["json_output"]:
            self.stdout.write(json_output({"models": rows}))
            return
        print_model_table(rows, self.stdout)
