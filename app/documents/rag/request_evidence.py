"""Audit the final request; preserve existing execution and external endpoint policy."""
from capabilities.deployment import load_deployment_snapshot
from capabilities.function_scope import assess_request_options


def audit_request_evidence(llm_config, payload, *, adapter_contract):
    resolution = getattr(llm_config, "provider_resolution", None)
    if not isinstance(resolution, dict) or resolution.get("source") != "server_runtime":
        return
    try:
        snapshot = load_deployment_snapshot()
        if snapshot.generation_id != resolution.get("deployment_generation_id"):
            raise ValueError("deployment changed")
        candidate = next((item for item in snapshot.function_providers.get("text.generate", [])
                          if item.get("model_installation_id") == resolution.get("model_installation_id")), None)
        result = assess_request_options(candidate.get("request_evidence_scope") if candidate else None,
                                        payload, adapter_contract=adapter_contract)
    except (OSError, ValueError):
        result = {"mode": "audit", "status": "revalidation_required", "reasons": ["deployment_scope_unavailable"]}
    result["backend_status"] = "not_assessed_at_request_scope_boundary"
    # Matching options alone is not evidence of the active backend version.
    resolution["request_evidence"] = result
    resolution["declaration_verification"] = resolution.get("declaration_verification", resolution.get("verification", ""))
    resolution["request_verification"] = "not_established" if result["status"] == "options_applicable" else "declared_unverified"
