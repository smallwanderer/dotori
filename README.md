# Dotori for Document

Dotori is a self-hosted document workspace for private file management, hybrid search, and retrieval-augmented generation (RAG). It runs as an operator-managed Docker Compose stack and keeps documents, indexes, and local AI runtimes on your server.

<p align="center">
  <img src="https://github.com/user-attachments/assets/263cba6d-04f6-49ba-9ccb-85481157539a" width="80%" alt="Dotori document workspace">
</p>

<p align="center">
  <img src="https://github.com/user-attachments/assets/d240cf34-1dfb-462e-8366-3f0d3e4a435f" width="80%" alt="Dotori RAG workspace">
</p>

한국어 문서는 [README.ko.md](README.ko.md)를 참고하세요. Detailed installation and runtime operations are covered in [WALKTHROUGH.md](WALKTHROUGH.md).

## Highlights

- Private file and folder management with authentication, trash, favorites, and recent files.
- Asynchronous parsing, chunking, and embedding for PDF, Office, HWP/HWPX, text, Markdown, HTML, and common image files.
- Dense and sparse hybrid retrieval backed by PostgreSQL and pgvector.
- RAG answers with source evidence, streaming output, cancellation, and job progress.
- Server-wide local LLM selection based on detected CPU, RAM, GPU, VRAM, and disk capacity.
- Automatic `llama.cpp` or vLLM runtime planning with `speed`, `balanced`, and `quality` presets.
- Three installation modes, from file storage only to a complete local RAG stack.
- Korean and English web interfaces and a sync API for external clients.

## Installation Modes

The installer asks for an operating mode. AI models and runtimes are configured for the server, not separately for each user.

| Mode | Services |
| --- | --- |
| Full local AI RAG | File management, parsing, embeddings, hybrid search, query processing, and local RAG generation |
| Hybrid/Search AI | File management, parsing, embeddings, and hybrid search without an answer-generation LLM |
| Basic | File management only |

In Full mode, the operator selects only `speed`, `balanced`, or `quality`. Dotori derives the model, backend, quantization, context length, concurrency, and memory policy from the detected hardware. GGUF and RAM-offload configurations use `llama.cpp`; compatible NVIDIA GPU and cluster configurations can use vLLM.

## Quick Start

### Requirements

- Docker Engine or Docker Desktop
- Docker Compose v2
- Python 3
- Git
- An NVIDIA driver and NVIDIA Container Toolkit when using the vLLM GPU runtime
- A Hugging Face token when a selected model requires authentication

Docker Desktop with the WSL2 backend is recommended on Windows.

### Run the installer

```bash
git clone https://github.com/smallwanderer/dotori.git
cd dotori
python install.py
```

The installer creates `.env`, detects the host hardware, lets you choose an operating mode, builds the required services with `docker-compose.yml`, and configures the selected RAG runtime. Open the service at:

```text
http://localhost/
```

Create an administrator account after the containers are running:

```bash
docker compose -f docker-compose.yml exec app python manage.py createsuperuser
```

On later starts, use `python install.py --run` or `start.bat` on Windows. To change the local RAG model or runtime explicitly, run:

```bash
python install.py --change-llm
```

For domain, HTTPS, and environment-specific setup, follow [WALKTHROUGH.md](WALKTHROUGH.md).

## How It Works

```text
Browser / sync client
        |
      Nginx
        |
   Django app -------------- PostgreSQL + pgvector
        |
      Redis
        |
        +-- embedding-worker  [parse, embed]
        +-- search-worker     [search]
        +-- query-worker      [query]
        +-- rag-worker        [rag] ---- selected local runtime
                                          |-- llama-rag (llama.cpp)
                                          `-- vllm-rag  (vLLM)
```

Django handles the web application, APIs, and task submission. Parsing, embedding, retrieval, query processing, and answer generation run in dedicated Celery queues. Only the selected RAG runtime should remain active. Its resolved configuration is stored in `data/config/llm_runtime.json`, and normal RAG requests reuse that saved configuration without probing hardware or selecting a model again.

## Common Commands

```bash
# Start or rebuild the installation stack
docker compose -f docker-compose.yml up --build

# Apply database migrations
docker compose -f docker-compose.yml exec app python manage.py migrate

# Follow one service's logs
docker compose -f docker-compose.yml logs -f rag-worker

# Inspect the saved LLM runtime configuration
docker compose -f docker-compose.yml exec app \
  python manage.py inspect_llm_runtime
```

## Data and Configuration

- `.env` configures the installer-managed stack; `.env.dev` is reserved for development-only runs.
- `data/uploads/`, `data/pgdata/`, `data/logs/`, and `data/config/` contain persistent local state and must not be committed.
- `data/config/llm_runtime.json` is generated during local RAG setup and is the server-wide runtime source of truth.
- The default embedding profile uses BGE-M3 with 1024-dimensional dense vectors and sparse lexical weights.
- Advanced retrieval and worker settings are available in `.env.example`; start with the defaults unless you are evaluating a specific workload.

## Development

The repository keeps application ownership in `files`, `accounts`, and `document_ai`. Heavy AI work is separated into processing, embedding, query-understanding, search, RAG, and installation/runtime modules. The production-like application image intentionally excludes test dependencies, so tests use the `test` profile in the default Compose file.

Before submitting a change, run focused tests for the affected module and then the full suite when practical:

```bash
docker compose --profile test run --rm test python manage.py check
docker compose --profile test run --rm test python -m pytest
```

## Project Status

Dotori is under active development. The current source includes the server-wide LLM installation service, hybrid retrieval, local and external RAG endpoint support, and the document workspace. Production operators should review their TLS, backup, monitoring, storage, model licensing, and hardware requirements before deployment.

## License

See [LICENSE](LICENSE).
