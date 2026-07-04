#!/bin/sh
set -eu

ARGS_FILE="${LLAMA_RAG_ARGS_FILE:-/config/llama_rag.args}"

if [ -s "$ARGS_FILE" ]; then
    set --
    while IFS= read -r arg || [ -n "$arg" ]; do
        set -- "$@" "$arg"
    done < "$ARGS_FILE"
fi

exec /app/llama-server "$@"
