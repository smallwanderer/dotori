# Query Parser CPU Runtime Comparison

Date: 2026-06-10

This note summarizes the current CPU-only comparison between `vllm-query-parser` and `llama-query-parser` for the query-understanding path.

## Scope

- Goal: compare two local query-parser runtimes under the same Django app flow
- Target task: `intent` + `semantic_query` extraction for the current `llm_query` path
- Test entrypoints:
  - direct `/v1/chat/completions` request from `app`
  - `app/tests/manual_query_pipeline.py --direct-parser`

## Runtime Setup

### vLLM

- Service: `vllm-query-parser`
- Engine: `vllm/vllm-openai-cpu`
- Model: `Qwen/Qwen2.5-0.5B-Instruct`
- Served model name: `query-parser`
- Context: `2048`
- Dtype: `bfloat16`
- CPU KV cache space: `1 GiB`
- Reserved CPU count: `1`
- OMP thread bind: `auto`

Observed process state:

- `VmRSS`: `917184 kB`
- `VmSize`: `5468536 kB`
- `Threads`: `68`

Observed engine log snippets:

- slow case: `Avg prompt throughput: 6.5 tokens/s`, `Avg generation throughput: 0.1 tokens/s`
- better case: `Avg prompt throughput: 90.2 tokens/s`, `Avg generation throughput: 6.0 tokens/s`

### llama.cpp

- Service: `llama-query-parser`
- Engine: `ghcr.io/ggml-org/llama.cpp:server`
- Model: `bartowski/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M`
- Alias: `query-parser`
- Context: `2048`
- Threads: `8`
- Parallel slots: `1`

Observed process state:

- `VmRSS`: `507492 kB`
- `VmSize`: `1134360 kB`
- `Threads`: `25`

Observed llama.cpp log snippets:

- host CPU: `13th Gen Intel(R) Core(TM) i5-1340P`
- projected host memory: `407 MiB`
- prompt cache enabled, size limit: `8192 MiB`
- `n_ctx = 2048`
- `n_slots = 1`

## Measured Results

### A. Short direct API request

Same payload:

- system: compact JSON only
- user: `지난주 업로드한 pdf 계약 문서에서 해지 조항 알려줘`
- `max_tokens=64`

Results:

- `vllm-query-parser`
  - `200 OK`
  - elapsed: about `14116.9 ms`
- `llama-query-parser`
  - `200 OK`
  - elapsed: about `12950.1 ms`

Interpretation:

- both were slow for a very small query-parser task
- llama.cpp was slightly faster in this narrow test

### B. Real parser path via `manual_query_pipeline.py`

Same payload:

- `--direct-parser`
- `--max-tokens 128`
- same Korean contract query

After fixes:

- `vllm-query-parser`
  - first token: about `20730.1 ms`
  - total: about `24495.9 ms`
  - status: `success`
  - result quality:
    - `intent=general_question`
    - `answer_mode=app_help`
    - `retrieval_required=false`
    - `semantic_query=해지 조항 알려줘`

- `llama-query-parser`
  - first token: about `5534.8 ms`
  - total: about `8646.4 ms`
  - status: `success`
  - result quality:
    - `intent=document_question`
    - `answer_mode=rag`
    - `retrieval_required=true`
    - `semantic_query=계약 문서 해지 조항`

### C. Concurrency behavior

Two concurrent requests were sent to `vllm-query-parser`.

Before fix:

- second request often failed quickly with:
  - `Query LLM parser is busy. Please retry shortly.`

After fix:

- both requests completed successfully
- the second request waited for the first one instead of failing immediately
- observed totals were high, around `20s` to `30s`, but no immediate `busy` fallback occurred

## Problems Found

### 1. `vllm-query-parser` busy fallback

Cause:

- query parser is guarded by Redis semaphore
- `QUERY_SEMAPHORE_COUNT=1`
- previous default `QUERY_SEMAPHORE_TIMEOUT=5`
- real parser latency was often much larger than `5s`

Effect:

- a valid second request could not wait long enough for the lock
- parser returned fallback even though the runtime itself was alive

### 2. `llama-query-parser` stream decoding issue

Cause:

- llama.cpp returned `text/event-stream` without an explicit charset
- `requests` treated the stream as `ISO-8859-1`
- Korean text inside stream chunks was decoded incorrectly

Effect:

- JSON looked broken or garbled
- parser fell back even when the model had actually produced useful fields

### 3. Partial JSON truncation

Cause:

- with `max_tokens=128`, the model sometimes emitted the important fields first and then got cut off inside `filters`

Effect:

- full `json.loads()` failed
- the old code discarded the whole output

### 4. `llama-query-parser` compose startup failure

Cause:

- compose command interpolation for `--threads` was not being passed correctly

Effect:

- container restarted with:
  - `error while handling argument "--threads": stoi`

## Fixes Applied

### Code

File: [app/document_ai/tasks.py](/mnt/c/users/a/desktop/Dotori/app/document_ai/tasks.py)

- switched query stream reading to raw bytes + explicit UTF-8 decode
- changed query semaphore timeout fallback to use `QUERY_REQUEST_TIMEOUT`
- release semaphore only if acquire actually succeeded
- added partial JSON salvage for:
  - `intent`
  - `semantic_query`
  - `retrieval_required`
  - `answer_mode`
  - `confidence`
  - `reason`

### Environment

Files:

- [.env.dev](/mnt/c/users/a/desktop/Dotori/.env.dev)
- [.env.example](/mnt/c/users/a/desktop/Dotori/.env.example)

Added:

- `QUERY_SEMAPHORE_COUNT=1`
- `QUERY_SEMAPHORE_TIMEOUT=180`

### Compose

File: [docker-compose.dev.yml](/mnt/c/users/a/desktop/Dotori/docker-compose.dev.yml)

- rewrote `llama-query-parser` command into argument-array form
- fixed runtime startup so `--threads 8` is passed as an actual integer argument

## Current Conclusion

For the current CPU-only local query-parser experiment:

- `llama-query-parser` is lighter on memory
- `llama-query-parser` is faster on first token and total completion for the tested query
- `llama-query-parser` currently produced the more useful `intent` and `semantic_query`
- `vllm-query-parser` is running correctly after semaphore fixes, but its classification quality on the tested query was worse

## Why This Result Happened

This result should not be read as "quantized llama.cpp is inherently smarter than the original model."

The more likely reasons are:

1. The current task is a very small structured-output task, not a general reasoning benchmark.
2. The current prompt is still relatively heavy for a `0.5B` model.
3. Small models are highly sensitive to:
   - chat template differences
   - stream behavior
   - truncation position
   - JSON formatting pressure
   - prompt wording
4. In this setup, llama.cpp emitted the important fields early enough that the parser could recover them.
5. In this setup, vLLM completed successfully but classified the tested question less accurately.

So the present outcome is about the current runtime-plus-prompt behavior, not a general statement that GGUF Q4 is better than the original model.
