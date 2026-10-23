import json
from pathlib import Path

from django.core.management.base import CommandError
from documents.embedding.candidates import resolve_candidate
from documents.parsers.candidate_validation import validate_parser_candidate
from documents.management.commands.validate_embedding_candidate import Command as EmbeddingCommand


class Command(EmbeddingCommand):
    help = "Validate a candidate tokenizer and Docling chunks in a one-off parser executor."

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("--document-file", type=Path)
        parser.add_argument("--token-headroom", type=int, default=128)
        parser.add_argument("--max-chunks", type=int, default=64)

    def handle(self, *args, **options):
        output = options["result_output"]
        if output and output.exists():
            raise CommandError("Use a new result-output path")
        try:
            if options.get("parser_report"):
                raise ValueError("Parser diagnostics produce a report; they do not consume --parser-report")
            if options["config_only"]:
                raise ValueError("Use validate_embedding_candidate --config-only for configuration validation")
            runtime = resolve_candidate(options)
            report = validate_parser_candidate(runtime, document_file=options["document_file"],
                headroom=options["token_headroom"], max_chunks=options["max_chunks"])
        except Exception as exc:
            report = {"passed": False, "checks": {"configuration": False},
                      "failures": [{"check": "configuration", "error": str(exc)}]}
        serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if output:
            with output.open("x", encoding="utf-8") as handle:
                handle.write(serialized)
        self.stdout.write(serialized, ending="")
        if not report["passed"]:
            raise CommandError("Parser candidate validation failed; see the JSON report")
