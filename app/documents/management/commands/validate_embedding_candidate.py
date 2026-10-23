"""Read-only candidate validation; execute in a one-off embedding executor."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from documents.embedding.candidate_validation import validate_candidate
from documents.embedding.candidates import resolve_candidate


class Command(BaseCommand):
    help = "Validate a candidate embedding profile without activating it or persisting vectors."
    requires_system_checks = []
    requires_migrations_checks = False

    def add_arguments(self, parser):
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--catalog-id")
        source.add_argument("--model-file", type=Path)
        parser.add_argument("--profile-file", type=Path)
        parser.add_argument("--result-output", type=Path, help="Optional new JSON report file; existing files are never overwritten.")
        parser.add_argument("--config-only", action="store_true", help="Validate configuration without loading any model.")
        parser.add_argument("--parser-report", type=Path, help="Successful candidate Parser report to validate through this model.")

    def handle(self, *args, **options):
        output = options["result_output"]
        if output and output.exists():
            raise CommandError("Use a new result-output path")
        try:
            runtime = resolve_candidate(options)
            if options["config_only"] and options["parser_report"]:
                raise ValueError("--parser-report requires execution validation")
            parser_report = json.loads(options["parser_report"].read_text(encoding="utf-8")) if options["parser_report"] else None
            report = validate_candidate(runtime, config_only=options["config_only"], parser_report=parser_report)
        except Exception as exc:
            report = {"schema_version": 1, "passed": False, "checks": {"configuration": False},
                      "failures": [{"check": "configuration", "error": str(exc)}],
                      "remaining_checks": ["model_execution", "retrieval_quality", "production_applicability"]}
        serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if output:
            try:
                with output.open("x", encoding="utf-8") as handle:
                    handle.write(serialized)
            except OSError as exc:
                raise CommandError(f"Cannot write candidate report: {exc}") from exc
        self.stdout.write(serialized, ending="")
        if not report["passed"]:
            raise CommandError("Candidate validation failed; see the JSON report")
