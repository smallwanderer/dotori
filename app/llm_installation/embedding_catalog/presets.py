from __future__ import annotations

# Preset -> catalog entry id. Which model answers a Speed/Balanced/Quality
# request is a product-level routing decision, not a property of the model
# itself, so it lives here in one place instead of on each profile JSON.
#
# These presets pick an embedding model only. LLM runtime planning has no
# equivalent knob: model choice and serving settings are derived from detected
# hardware alone (see llm_installation/selection.py).
EMBEDDING_PRESETS: dict[str, str] = {
    "speed": "harrier-270m",
    "balanced": "bge-m3-hybrid",
    "quality": "gte-qwen2-1.5b",
}
