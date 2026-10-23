"""Runtime option-scope inspection from the deployment snapshot, without catalog I/O."""
import math


_METADATA = {"request_timeout_seconds", "tested_token_cap", "runtime_fingerprint"}
_PROTOCOL = {"model", "messages", "input", "instructions", "stream", "stream_options"}


def project_request_scope(record):
    return {"schema_version": 1, "function_id": record.function_id,
            "evidence_ref": record.evidence_id, "adapter_contracts": sorted({t.adapter_contract for t in record.targets}),
            "backend_targets": [{"backend_id": t.backend_id, "backend_version": t.backend_version} for t in record.targets],
            "inference_options": {k: v for k, v in record.inference_options.items() if k not in _METADATA},
            "option_constraints": record.applicability_profile.model_dump(mode="json", exclude_none=True)["option_constraints"]
                if record.applicability_profile else {}}


def validate_request_scope(scope):
    if (not isinstance(scope, dict) or scope.get("schema_version") != 1
            or scope.get("function_id") != "text.generate" or not isinstance(scope.get("evidence_ref"), str)
            or not isinstance(scope.get("inference_options"), dict)
            or not isinstance(scope.get("option_constraints"), dict)
            or not isinstance(scope.get("adapter_contracts"), list)
            or not isinstance(scope.get("backend_targets"), list)):
        raise ValueError("Invalid request evidence scope")
    for key, rule in scope["option_constraints"].items():
        if key not in scope["inference_options"] or not isinstance(rule, dict):
            raise ValueError("Invalid request option constraint")
        if "allowed_values" in rule:
            if not isinstance(rule["allowed_values"], list) or not rule["allowed_values"]:
                raise ValueError("Invalid allowed option values")
        elif (type(rule.get("minimum")) not in (int, float) or type(rule.get("maximum")) not in (int, float)
              or not math.isfinite(rule["minimum"]) or not math.isfinite(rule["maximum"])
              or rule["minimum"] > rule["maximum"]):
            raise ValueError("Invalid request option range")


def _equal(actual, expected):
    if type(actual) in (int, float) and type(expected) in (int, float):
        return math.isfinite(actual) and math.isfinite(expected) and actual == expected
    return type(actual) is type(expected) and actual == expected


def assess_request_options(scope, payload, *, adapter_contract):
    result = {"mode": "audit", "status": "revalidation_required", "reasons": []}
    if scope is None:
        return {**result, "reasons": ["legacy_scope_unavailable"]}
    validate_request_scope(scope)
    result["evidence_ref"] = scope["evidence_ref"]
    if adapter_contract not in scope["adapter_contracts"]:
        result["reasons"].append("request_adapter_mismatch")
    options = {k: v for k, v in payload.items() if k not in _PROTOCOL}
    if "max_output_tokens" in options:
        if "max_tokens" in options:
            result["reasons"].append("ambiguous_token_limit")
        options["max_tokens"] = options.pop("max_output_tokens")
    if type(payload.get("stream", False)) is not bool:
        result["reasons"].append("invalid_stream_option")
    options["requests"] = ["stream" if payload.get("stream", False) else "nonstream"]
    expected = scope["inference_options"]
    if set(options) != set(expected):
        result["reasons"].append("request_option_keys_mismatch")
    for key in sorted(set(options) & set(expected)):
        actual = options[key]
        rule = scope["option_constraints"].get(key)
        if rule and "allowed_values" in rule:
            valid = any(_equal(actual, value) for value in rule["allowed_values"])
        elif rule:
            valid = type(actual) in (int, float) and math.isfinite(actual) and rule["minimum"] <= actual <= rule["maximum"]
        elif key == "requests":
            valid = isinstance(expected[key], list) and all(value in expected[key] for value in actual)
        else:
            valid = _equal(actual, expected[key])
        if not valid:
            result["reasons"].append(f"request_option_mismatch:{key}")
    if not result["reasons"]:
        result["status"] = "options_applicable"
    return result
