"""Fit model input without discarding a required profile/model prompt."""


def fit_embedding_text(text, tokenizer, *, max_tokens, prompt="", required_prefix=""):
    if max_tokens <= 0:
        raise ValueError("Embedding token cap must be positive")
    def count(body):
        return len(tokenizer(prompt + body, truncation=False)["input_ids"])
    original_count = count(text)
    if original_count <= max_tokens:
        return text, original_count, False
    if count(required_prefix) >= max_tokens:
        raise ValueError("Embedding prompt leaves no token budget for document content")
    low, high = len(required_prefix), len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if count(text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    bounded = text[:low].rstrip()
    if len(bounded) <= len(required_prefix.rstrip()) or count(bounded) > max_tokens:
        raise ValueError("Cannot fit nonempty content into the embedding token budget")
    return bounded, count(bounded), True


def sentence_transformer_prompt(model, options):
    if "prompt" in options:
        return options["prompt"]
    name = options.get("prompt_name") or getattr(model, "default_prompt_name", None)
    if not name:
        return ""
    prompts = getattr(model, "prompts", {})
    if name not in prompts:
        raise ValueError(f"Unknown model prompt: {name}")
    return prompts[name]
