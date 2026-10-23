from __future__ import annotations

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from llm_installation.catalog import get_catalog_entry
from llm_installation.function_evaluator import evaluate_text_generate_contract


BACKEND_IDS = ("vllm-cuda", "llamacpp-cpu", "llamacpp-gpu-offload")


class Command(BaseCommand):
    help = "Run a synthetic text.generate contract evaluation for a selected catalog artifact."

    def add_arguments(self, parser):
        parser.add_argument("--base-url", required=True)
        parser.add_argument("--artifact-id", required=True)
        parser.add_argument("--backend-id", required=True, choices=BACKEND_IDS)
        parser.add_argument("--backend-version", required=True)
        parser.add_argument("--adapter-contract", required=True)
        parser.add_argument("--artifact-sha256", default="")
        parser.add_argument("--evaluation-id", required=True)
        parser.add_argument("--result-output", required=True)
        parser.add_argument("--evidence-output", required=True)
        parser.add_argument("--timeout", type=float, default=120)

    def handle(self, *args, **options):
        entry = get_catalog_entry(options["artifact_id"])
        if entry is None:
            raise CommandError(f"Unknown catalog artifact: {options['artifact_id']}")
        backend_id = options["backend_id"]
        if backend_id not in entry.backend_profiles:
            raise CommandError(
                f"Artifact {entry.id} does not support backend {backend_id}."
            )

        catalog_digest = entry.artifact.sha256 or ""
        supplied_digest = options["artifact_sha256"].strip().lower()
        if catalog_digest and supplied_digest and catalog_digest.lower() != supplied_digest:
            raise CommandError("Provided artifact SHA-256 does not match the catalog.")
        artifact_digest = (catalog_digest or supplied_digest).lower()
        if not re.fullmatch(r"[a-fA-F0-9]{64}", artifact_digest):
            raise CommandError(
                "A SHA-256 digest is required via catalog artifact.sha256 or --artifact-sha256."
            )

        result_path = Path(options["result_output"]).expanduser()
        evidence_path = Path(options["evidence_output"]).expanduser()
        if result_path.resolve() == evidence_path.resolve():
            raise CommandError("Result and evidence outputs must be different files.")
        for path in (result_path, evidence_path):
            if path.exists():
                raise CommandError(f"Output already exists; refusing to overwrite: {path}")

        try:
            result = evaluate_text_generate_contract(
                base_url=options["base_url"],
                model=entry.id,
                model_id=entry.model_id,
                model_revision=entry.hf.revision,
                artifact_id=entry.id,
                artifact_revision=entry.hf.revision,
                artifact_digest=artifact_digest,
                backend_id=backend_id,
                backend_version=options["backend_version"],
                adapter_contract=options["adapter_contract"],
                evaluation_id=options["evaluation_id"],
                timeout=options["timeout"],
            )
        except (ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc

        report = result.as_dict()
        report["target"] = {
            "catalog_model_id": entry.model_id,
            "model_revision": entry.hf.revision,
            "artifact_id": entry.id,
            "artifact_revision": entry.hf.revision,
            "artifact_digest": artifact_digest,
            "backend_id": backend_id,
            "backend_version": options["backend_version"],
            "adapter_contract": options["adapter_contract"],
        }
        if not result.passed:
            self._write_new_json(result_path, report)
            self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2))
            raise CommandError(
                f"text.generate contract evaluation failed; report saved to {result_path}"
            )

        evidence_payload = result.evidence.model_dump(mode="json")
        self._write_new_json(result_path, report)
        self._write_new_json(evidence_path, evidence_payload)
        self.stdout.write(
            self.style.SUCCESS(
                f"text.generate contract passed; report={result_path} evidence={evidence_path}"
            )
        )

    @staticmethod
    def _write_new_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8", newline="\n") as output:
                json.dump(payload, output, ensure_ascii=False, indent=2, sort_keys=True)
                output.write("\n")
        except FileExistsError as exc:
            raise CommandError(f"Refusing to overwrite existing output: {path}") from exc
