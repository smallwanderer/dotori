#!/bin/sh
set -eu

ARGS_FILE="${VLLM_RAG_ARGS_FILE:-/config/vllm_rag.args}"

if [ -s "$ARGS_FILE" ]; then
    set --
    while IFS= read -r arg || [ -n "$arg" ]; do
        set -- "$@" "$arg"
    done < "$ARGS_FILE"
fi

exec python3 -m vllm.entrypoints.openai.api_server "$@"
