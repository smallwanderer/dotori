"""Read-only applicability inspection; never promotes a deployment candidate."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from llm_installation.function_applicability import ObservedFunctionExecution, assess_evidence_applicability
from llm_installation.function_catalog import load_function_evidence


class Command(BaseCommand):
    help = "Compare a catalog evidence record with execution-server observations without changing routing."

    def add_arguments(self, parser):
        parser.add_argument("--evidence-ref", required=True)
        parser.add_argument("--runtime-generation", required=True)
        parser.add_argument("--observation")
        parser.add_argument("--result-output")

    def handle(self, *args, **options):
        try:
            record = load_function_evidence(options["evidence_ref"])
            observed = None
            if options["observation"]:
                observed = ObservedFunctionExecution.model_validate(json.loads(
                    Path(options["observation"]).read_text(encoding="utf-8")))
            result = assess_evidence_applicability(record, observed,
                expected_runtime_generation_id=options["runtime_generation"])
            serialized = json.dumps(result, ensure_ascii=False, indent=2)
            if options["result_output"]:
                with Path(options["result_output"]).open("x", encoding="utf-8") as output:
                    output.write(serialized + "\n")
        except (OSError, ValueError) as exc:
            raise CommandError("Unable to assess evidence from the specified inputs") from exc
        self.stdout.write(serialized)
        if result["status"] != "applicable":
            raise CommandError("Evidence requires revalidation; deployment was not changed")
