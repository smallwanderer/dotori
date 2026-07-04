# LLM Runtime Validation Commands

## Installation-Time Detection

```bash
python manage.py detect_llm_runtime --write --priority balanced
python manage.py detect_llm_runtime --write --priority speed
python manage.py detect_llm_runtime --write --priority quality
```

## Endpoint Validation

```bash
python manage.py detect_llm_runtime --write --check-endpoint
python manage.py detect_llm_runtime --write --check-endpoint --smoke-test
```

## Catalog Inspection

```bash
python install.py --list-llm-models
python install.py --search-llm qwen --json-output
python install.py --show-llm qwen2.5-7b-instruct-q4_k_m
```

Inside the container:

```bash
python manage.py llm_model_catalog list
python manage.py llm_model_catalog search qwen --json-output
```

## Runtime Inspection

```bash
python manage.py inspect_llm_runtime
python manage.py inspect_llm_runtime --live
python manage.py inspect_llm_runtime --live --check-endpoint
python manage.py inspect_llm_runtime --live --check-endpoint --smoke-test
```

## Smoke Test Target

Smoke test may check:

```text
POST {base_url}/v1/chat/completions
```

Expected validation data:

- HTTP status code
- response time
- whether selected model is usable
- short failure message
