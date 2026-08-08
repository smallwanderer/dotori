# Dotori Documents

Dotori is a self-hosted document workspace for document management, hybrid search, and retrieval-augmented generation (RAG) on a single server. Documents, search indexes, and local AI runtimes remain in an operator-managed Docker Compose environment.

<p align="center">
  <img src="https://github.com/user-attachments/assets/1f711ecb-db68-4dd7-a6a2-435e523f3bb6" width="80%" alt="Dotori document workspace">
</p>


For Korean documentation, see [README.ko.md](README.ko.md). For detailed installation and operations, see the [installation guide](documents/installation-guide.md).

## Key Features

- Multiple accounts with secure separation of files and document features
- Folder and file management with authentication, trash, favorites, and recent files
- Analysis of PDF, HWP, DOCX, and other text-based document formats
- Natural-language document search
- Local RAG that answers questions from document contents without sending data to an external provider
- Guided installation and use of a local LLM selected for the server's hardware
- Three installation modes, from file management only to a complete local RAG stack
- Korean and English web interfaces, with support for external AI models

> [!CAUTION]
> The operator must select the local LLM. AI models and runtimes are managed as server-wide settings, not per-user settings.
>
> When an external LLM such as ChatGPT or Claude is selected, document content may be sent to the external provider.

## Installation Modes

The included installation assistant, `start.bat`, lets operators set up the server without writing code. See the [installation guide](documents/installation-guide.md) for details.

The installer provides the following options:

1. Optional feature activation, including enabling or disabling natural-language search and RAG
2. Local LLM installation guidance
3. Server environment configuration guidance
4. External-access domain configuration

## Quick Start

### Requirements

- Docker Engine or Docker Desktop
- Python 3.x
- An NVIDIA driver and NVIDIA Container Toolkit when using the vLLM GPU runtime
- A Hugging Face token when the selected model requires authentication

Docker Desktop with the WSL2 backend is recommended on Windows.

## System Architecture

```text
      Nginx
        |
     Web App -------------- Database
        |
    Queue Server
        |
        +-- embedding-worker  [parse, embed]
        +-- search-worker     [search]
        +-- query-worker      [query]
        +-- rag-worker        [rag] ---- selected local runtime
                                          |-- llama-rag (llama.cpp)
                                          `-- vllm-rag  (vLLM)
```

The queue server and its workers process tasks asynchronously according to the configured concurrency.

## Data and Configuration

- `.env.example` contains the primary operational settings. Copy it to `.env` and adjust it as needed.
- `.env.example` also contains controls for advanced features.
- All files saved through Dotori are stored on the local server filesystem. Dotori does not currently provide a separate backup feature.
- `data/config/llm_runtime.json` is generated during local RAG setup and serves as the server-wide source of truth for the selected runtime.

## Development

The repository's primary application boundaries are `files`, `accounts`, and `document_ai`. Heavy AI work is separated into processing, embedding, query-understanding, search, RAG, installation, and runtime modules.

The repository includes tests for selected behavior:

```bash
docker compose --profile test run --rm test python manage.py check
docker compose --profile test run --rm test python -m pytest
```

## Project Status

Dotori is under active development. The current source provides the features described above and includes experimental functionality. Please report bugs and feature requests through an issue or by email.

## License

See [LICENSE](LICENSE).
