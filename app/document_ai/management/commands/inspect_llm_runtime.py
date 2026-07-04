from __future__ import annotations

from dataclasses import replace

from django.core.management.base import BaseCommand, CommandError

from document_ai.llm_installation_helper.catalog import get_rag_model_catalog
from document_ai.llm_installation_helper.config_store import get_llm_runtime_config_path, load_llm_runtime_config
from document_ai.llm_installation_helper.planner import PRIORITY_PRESETS, assess_catalog_entry
from document_ai.llm_installation_helper.router import (
    LLMRuntimeNotConfigured,
    get_configured_server_rag_target,
    resolve_server_rag_target,
)
from document_ai.llm_installation_helper.runtime_probe import probe_server_runtime


class Command(BaseCommand):
    help = "Inspect the self-hosted server LLM runtime profile and selected RAG model target."

    def add_arguments(self, parser):
        parser.add_argument(
            "--cluster-mode",
            action="store_true",
            help="Resolve live candidates for clustered/distributed serving.",
        )
        parser.add_argument(
            "--priority",
            choices=PRIORITY_PRESETS,
            default="balanced",
            help="Priority used for live orchestration previews.",
        )
        parser.add_argument(
            "--check-endpoint",
            action="store_true",
            help="With --live, call /v1/models on candidate endpoints before selecting a target.",
        )
        parser.add_argument(
            "--live",
            action="store_true",
            help="Probe the current server environment instead of only reading the persisted config.",
        )
        parser.add_argument(
            "--smoke-test",
            action="store_true",
            help="With --live, send a short chat completion request to validate the selected candidate.",
        )

    def handle(self, *args, **options):
        if not options["live"]:
            config_path = get_llm_runtime_config_path()
            config = load_llm_runtime_config(config_path)
            try:
                target = get_configured_server_rag_target()
            except LLMRuntimeNotConfigured as exc:
                raise CommandError(str(exc)) from exc
            self.stdout.write("Persisted LLM runtime config")
            self.stdout.write(f"path: {config_path}")
            self.stdout.write(f"exists: {bool(config)}")
            if config.get("generated_at"):
                self.stdout.write(f"generated_at: {config['generated_at']}")
            self.stdout.write("")
            self.stdout.write("Configured RAG target")
            self.stdout.write(f"endpoint_name: {target.endpoint_name}")
            self.stdout.write(f"base_url: {target.base_url}")
            self.stdout.write(f"model: {target.model}")
            self.stdout.write(f"runtime: {target.runtime}")
            self.stdout.write(f"fallback_used: {target.fallback_used}")
            self.stdout.write(f"reason: {target.reason}")
            self.stdout.write(f"priority_preset: {target.priority_preset}")
            if target.serving_profile:
                self.stdout.write(f"serving_profile: {target.serving_profile}")
            diagnostics = config.get("diagnostics", {}) if isinstance(config, dict) else {}
            candidates = diagnostics.get("candidates", []) if isinstance(diagnostics, dict) else []
            if candidates:
                self.stdout.write("")
                self.stdout.write("Saved candidate diagnostics")
                for candidate in candidates:
                    status = "selected" if candidate.get("selected") else "rejected"
                    reason = candidate.get("rejected_reason") or "-"
                    self.stdout.write(
                        f"- {status}: {candidate.get('runtime')} / {candidate.get('model')} "
                        f"@ {candidate.get('base_url')} reason={reason}"
                    )
            return

        profile = probe_server_runtime()
        if options["cluster_mode"]:
            profile = replace(profile, cluster_mode=True)
        catalog = get_rag_model_catalog()
        target = resolve_server_rag_target(
            profile=profile,
            catalog=catalog,
            check_endpoint=options["check_endpoint"],
            smoke_test=options["smoke_test"],
            priority_preset=options["priority"],
        )

        self.stdout.write("Server runtime profile")
        self.stdout.write(f"cpu_count: {profile.cpu_count}")
        self.stdout.write(f"ram_mb: {profile.ram_mb}")
        self.stdout.write(f"has_gpu: {profile.has_gpu}")
        self.stdout.write(f"gpu_name: {profile.gpu_name or '-'}")
        self.stdout.write(f"gpu_vram_mb: {profile.gpu_vram_mb}")
        self.stdout.write(f"cuda_available: {profile.cuda_available}")
        self.stdout.write(f"disk_free_mb: {profile.disk_free_mb}")
        self.stdout.write(f"container_memory_limit_mb: {profile.container_memory_limit_mb}")
        self.stdout.write(f"cuda_driver_version: {profile.cuda_driver_version or '-'}")
        self.stdout.write(f"driver_supported_cuda_version: {profile.driver_supported_cuda_version or '-'}")
        self.stdout.write(f"docker_available: {profile.docker_available}")
        self.stdout.write(f"docker_compose_available: {profile.docker_compose_available}")
        self.stdout.write(f"docker_service_count: {len(profile.docker_services or [])}")
        self.stdout.write(f"platform: {profile.platform}")
        self.stdout.write("")

        self.stdout.write("Catalog candidates")
        for entry in catalog:
            plan = assess_catalog_entry(entry, profile)
            if plan.memory_estimate is None or plan.memory_placement is None:
                self.stdout.write(
                    f"- unavailable: {entry.id} ({plan.fit_reason})"
                )
                continue
            self.stdout.write(
                f"- {plan.runtime}: {entry.id} "
                f"(priority={entry.priority}, logical_total_memory_mb={plan.memory_estimate.logical_total_memory_mb}, "
                f"required_ram_mb={plan.memory_placement.required_ram_mb}, "
                f"required_vram_per_gpu_mb={list(plan.memory_placement.required_vram_per_gpu_mb)}, "
                f"enabled={entry.enabled})"
            )
        self.stdout.write("")

        status = "fallback" if target.fallback_used else "selected"
        self.stdout.write(self.style.SUCCESS(f"RAG target: {status}"))
        self.stdout.write(f"endpoint_name: {target.endpoint_name}")
        self.stdout.write(f"base_url: {target.base_url}")
        self.stdout.write(f"model: {target.model}")
        self.stdout.write(f"runtime: {target.runtime}")
        self.stdout.write(f"reason: {target.reason}")
        if target.endpoint_status:
            self.stdout.write(f"endpoint_ok: {target.endpoint_status.ok}")
            self.stdout.write(f"endpoint_message: {target.endpoint_status.message}")
        if target.health_status:
            self.stdout.write(f"health_ok: {target.health_status.ok}")
            self.stdout.write(f"health_message: {target.health_status.message}")
        if target.smoke_status:
            self.stdout.write(f"smoke_ok: {target.smoke_status.ok}")
            self.stdout.write(f"smoke_message: {target.smoke_status.message}")
