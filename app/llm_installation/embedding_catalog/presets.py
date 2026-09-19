from __future__ import annotations

# Preset -> catalog entry id. Which model answers a Speed/Balanced/Quality
# request is a product-level routing decision, not a property of the model
# itself, so it lives here in one place instead of on each profile JSON --
# the same reason planner.py defines PresetPolicy centrally for LLM runtimes
# rather than letting each LLM model claim a preset for itself.
EMBEDDING_PRESETS: dict[str, str] = {
    "speed": "harrier-270m",
    "balanced": "bge-m3-hybrid",
    "quality": "gte-qwen2-1.5b",
}
