#!/bin/sh
set -eu

ARGS_FILE="${RAG_RUNTIME_ARGS_FILE:-/runtime/runtime.args}"

if [ -s "$ARGS_FILE" ]; then
    set --
    carriage_return="$(printf '\r')"
    while IFS= read -r arg || [ -n "$arg" ]; do
        arg="${arg%"$carriage_return"}"
        set -- "$@" "$arg"
    done < "$ARGS_FILE"
fi

exec $RAG_RUNTIME_EXEC "$@"
