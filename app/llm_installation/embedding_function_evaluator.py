"""Sanitized offline HTTP probes; no model imports or evidence promotion."""
import math
from datetime import datetime, timezone
import requests
from llm_installation.function_evaluator import _normalize_base_url

FIXTURES = ("Dotori stores local documents.", "Documents can be searched.")

def evaluate_text_embed_contract(*, base_url, runtime, model_id, backend_version,
                                 evaluation_id, token="", timeout=120, post=None, token_fixture=None):
    url = _normalize_base_url(base_url) + "/embed/"
    if not model_id or not backend_version or not evaluation_id or timeout <= 0:
        raise ValueError("Identity and positive timeout required")
    post = post or requests.post
    checks, vectors = {}, {}
    calls = [("query", {"input_type": "query", "text": FIXTURES[0]}),
             ("repeat", {"input_type": "query", "text": FIXTURES[0]}),
             ("document", {"input_type": "document", "text": FIXTURES[0]}),
             ("batch", {"input_type": "document", "texts": list(FIXTURES)})]
    if token_fixture:
        long_text, short_text, cap = token_fixture
        calls.extend([(name, {"input_type": "document", "text": text, "max_length": cap})
                      for name, text in [("token_long", long_text), ("token_short", short_text)]])
    for name, body in calls:
        body.setdefault("max_length", min(32, runtime.max_tokens))
        for suffix in ("http", "identity", "vectors"):
            checks[name + "_" + suffix] = False
        try:
            response = post(url, json=body,
                headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=timeout)
            checks[name + "_http"] = response.status_code == 200
            if not checks[name + "_http"]:
                continue
            payload = response.json()
            if not isinstance(payload, dict):
                continue
            checks[name + "_identity"] = payload.get("runtime_fingerprint") == runtime.runtime_fingerprint
            items = payload.get("results") if name == "batch" else [payload]
            if not isinstance(items, list) or len(items) != (2 if name == "batch" else 1):
                continue
            valid = True
            for item in items:
                if not isinstance(item, dict):
                    valid = False
                    break
                dense = item.get("dense_vector")
                if not isinstance(dense, list) or len(dense) != runtime.dimension or not all(
                    type(v) in (int, float) and math.isfinite(v) for v in dense):
                    valid = False
                    break
                norm = math.sqrt(sum(v*v for v in dense))
                sparse = item.get("sparse_vector", {})
                valid = valid and norm > 0 and (not runtime.normalize_embeddings or abs(norm-1) < 1e-3)
                valid = valid and item.get("model_name") == runtime.model_id and item.get("backend") == runtime.provider and item.get("dimension") == runtime.dimension
                valid = valid and isinstance(sparse, dict) and all(isinstance(k, str) and type(v) in (int, float) and math.isfinite(v) for k, v in sparse.items())
                valid = valid and (not runtime.supports_sparse or bool(sparse))
            checks[name + "_vectors"] = bool(valid)
            if valid:
                vectors[name] = [item["dense_vector"] for item in items]
        except (requests.RequestException, ValueError, TypeError, OverflowError):
            pass  # Exceptions may contain secrets or endpoint URLs.
    for key, left, right in [("reproducible", "query", "repeat"), ("batch_consistent", "document", "batch")]:
        checks[key] = left in vectors and right in vectors and all(abs(a-b) <= 1e-4 for a,b in zip(vectors[left][0], vectors[right][0]))
    if token_fixture:
        checks["token_limit"] = "token_long" in vectors and "token_short" in vectors and all(
            abs(a-b) <= 1e-4 for a,b in zip(vectors["token_long"][0], vectors["token_short"][0]))
    return dict(evaluation_id=evaluation_id, function_id="text.embed", passed=all(checks.values()),
        checks=checks, failures=[k for k,v in checks.items() if not v], evidence=None,
        executed_at=datetime.now(timezone.utc).isoformat(), sample_count=7 if token_fixture else 5,
        target=dict(model_id=model_id, model_revision=runtime.model_revision, profile_id=runtime.catalog_id,
            backend_id=runtime.provider, backend_version=backend_version, runtime_fingerprint=runtime.runtime_fingerprint),
        remaining_checks=(["token_limit"] if not token_fixture else []) + ["index_store_compatibility", "loaded_artifact_identity"])
