# Self-Hosting Initial Presets

Dotori should be configured once for the host machine, then started normally.

The intended flow is:

1. Choose an initial preset based on CPU/GPU/RAM and whether local or external LLMs are used.
2. Apply that preset to `.env.dev` / `.env` and the matching compose override in a later setup step.
3. Run Docker Compose without selecting services manually each time.

```bash
docker compose -f docker-compose.dev.yml up -d
```

All services defined by the selected compose configuration should start together.

## Preset Direction

### CPU Local

Use when the machine has enough CPU/RAM for local llama.cpp runtimes but no practical GPU runtime.

Expected runtime choices:

- query parser: `llama-query-parser`
- RAG model: `llama-rag`
- query metadata/ORM: usually disabled or reduced

Important env direction:

```env
QUERY_PARSER_BASE_URL=http://llama-query-parser:8080
QUERY_PARSER_REQUEST_MODEL=query-parser
QUERY_PARSER_INCLUDE_METADATA=0
QUERY_PIPELINE_ENABLED=1
RAG_LLM_URL=http://llama-rag:8080
```

### GPU Local

Use when the machine can run vLLM or another GPU-oriented runtime.

Expected runtime choices:

- query parser: `vllm-query-parser`
- RAG model: local GPU runtime or `llama-rag`, depending on the selected model
- query metadata/ORM: can be enabled if latency is acceptable

Important env direction:

```env
QUERY_PARSER_BASE_URL=http://vllm-query-parser:8080
QUERY_PARSER_REQUEST_MODEL=query-parser
QUERY_PARSER_INCLUDE_METADATA=1
QUERY_PIPELINE_ENABLED=1
```

### External LLM

Use when local model serving should not run on the host.

Expected runtime choices:

- local model containers are omitted in the selected compose override
- user-configured LLM endpoints are stored and selected through the application
- query parsing can be disabled, local, or pointed at an external compatible endpoint

Important env direction:

```env
QUERY_LLM_ENABLED=0
```

or, if using an external query parser endpoint:

```env
QUERY_LLM_ENABLED=1
QUERY_PARSER_BASE_URL=https://your-openai-compatible-endpoint
QUERY_PARSER_REQUEST_MODEL=your-model
```

## Current State

Current compose files no longer use Compose profiles. `docker compose up` starts every service defined in that compose file.

The next migration step should create preset-specific compose/env files, for example:

- `docker-compose.dev.cpu.yml`
- `docker-compose.dev.gpu.yml`
- `docker-compose.dev.external.yml`
- `.env.dev.cpu.example`
- `.env.dev.gpu.example`
- `.env.dev.external.example`

This keeps startup simple while still allowing the initial setup to match the user's machine.
