from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, is_dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import django


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model

from document_ai.search.query_frontend import RetrievalQueryPlan, prepare_retrieval_query
from document_ai.tasks import parse_user_query


DEFAULT_QUERIES = [
    "지난주 업로드한 pdf 계약 문서에서 해지 조항 알려줘",
    "안녕, 너는 무엇을 할 수 있어?",
    "양자역학이 뭐야?",
    "최근 올린 hwp 보도자료 요약해줘",
    "이 앱에서 문서 검색은 어떻게 해?",
]


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "pk"):
        return getattr(value, "pk")
    return value


def _resolve_owner(email: str | None):
    if not email:
        return None
    User = get_user_model()
    try:
        return User.objects.get(email=email)
    except User.DoesNotExist:
        raise SystemExit(f"Owner not found: {email}")


def _plan_to_dict(plan: RetrievalQueryPlan) -> dict[str, Any]:
    query_log = plan.query_log
    return {
        "raw_query": plan.raw_query,
        "mode": plan.mode,
        "source": plan.source,
        "intent": plan.intent,
        "answer_mode": plan.answer_mode,
        "retrieval_required": plan.retrieval_required,
        "confidence": plan.confidence,
        "retrieval_query": plan.retrieval_query,
        "warnings": plan.warnings,
        "metadata": plan.metadata,
        "query_log_id": query_log.id if query_log else None,
    }


def _print_plan(plan: RetrievalQueryPlan, *, elapsed_ms: float) -> None:
    print(f"\n{'=' * 72}")
    print(f"[입력] {plan.raw_query} (mode={plan.mode}, source={plan.source}, {elapsed_ms:.1f}ms)")
    print(f"{'=' * 72}")

    print("\n[1] Intent / Retrieval")
    print(f" - intent             : {plan.intent}")
    print(f" - answer_mode        : {plan.answer_mode}")
    print(f" - retrieval_required : {plan.retrieval_required}")
    print(f" - confidence         : {plan.confidence}")
    print(f" - semantic_query     : {plan.retrieval_query!r}")
    print(f" - query_log_id       : {plan.query_log.id if plan.query_log else '-'}")

    classification = plan.metadata.get("classification") or {}
    if classification.get("reason"):
        print(f" - reason             : {classification.get('reason')}")

    warnings = plan.warnings or []
    if warnings:
        print("\n[2] Warnings")
        for item in warnings:
            print(f" - {item.get('code')}: {item.get('message')}")

    print("\n[3] QueryDSL Metadata")
    filters = plan.metadata.get("filters") or []
    sorts = plan.metadata.get("sorts") or []
    target_scopes = plan.metadata.get("target_scopes") or []
    if filters:
        for item in filters:
            print(
                " - filter: "
                f"{item.get('scope')}.{item.get('field')} "
                f"{item.get('operator')} {item.get('value')!r} "
                f"(source={item.get('source_text')!r}, confidence={item.get('confidence')})"
            )
    else:
        print(" - filters: []")
    print(f" - sorts         : {json.dumps(sorts, ensure_ascii=False, default=str)}")
    print(f" - target_scopes : {json.dumps(target_scopes, ensure_ascii=False, default=str)}")

    print("\n[4] ORM")
    print(json.dumps(plan.metadata.get("orm") or {}, ensure_ascii=False, indent=2, default=str))


def run_frontend_query(query: str, *, mode: str, owner) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    plan = prepare_retrieval_query(query, mode=mode, owner=owner)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {"kind": "frontend", "plan": plan, "result": _plan_to_dict(plan)}, elapsed_ms


def run_direct_parser(query: str, *, mode: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    result = parse_user_query(query, mode)
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {"kind": "direct_parser", "result": result}, elapsed_ms


def run_once(query: str, *, args, owner) -> dict[str, Any]:
    if args.frontend_mode:
        os.environ["QUERY_UNDERSTANDING_FRONTEND_MODE"] = args.frontend_mode
    if args.query_timeout is not None:
        os.environ["QUERY_UNDERSTANDING_REQUEST_TIMEOUT"] = str(args.query_timeout)
    if args.max_tokens is not None:
        os.environ["QUERY_UNDERSTANDING_MAX_TOKENS"] = str(args.max_tokens)
    if args.query_url:
        os.environ["QUERY_UNDERSTANDING_PARSER_BASE_URL"] = args.query_url
        os.environ["QUERY_UNDERSTANDING_LLM_URL"] = args.query_url
    if args.query_model:
        os.environ["QUERY_UNDERSTANDING_PARSER_MODEL"] = args.query_model
        os.environ["QUERY_UNDERSTANDING_LLM_MODEL"] = args.query_model

    if args.direct_parser:
        payload, elapsed_ms = run_direct_parser(query, mode=args.mode)
        if args.json:
            return {**payload, "elapsed_ms": elapsed_ms}
        print(f"\n{'=' * 72}")
        print(f"[direct parser] {query} (mode={args.mode}, {elapsed_ms:.1f}ms)")
        print(f"{'=' * 72}")
        print(json.dumps(payload["result"], ensure_ascii=False, indent=2, default=str))
        return {**payload, "elapsed_ms": elapsed_ms}

    payload, elapsed_ms = run_frontend_query(query, mode=args.mode, owner=owner)
    if args.json:
        return {**payload, "elapsed_ms": elapsed_ms}
    _print_plan(payload["plan"], elapsed_ms=elapsed_ms)
    return {**payload, "elapsed_ms": elapsed_ms}


def _stress_queries(base_queries: list[str], count: int) -> list[str]:
    return [base_queries[index % len(base_queries)] for index in range(count)]


def run_stress(queries: list[str], *, args, owner) -> list[dict[str, Any]]:
    work_items = _stress_queries(queries, args.stress)
    results = []
    started = time.perf_counter()
    quiet_args = SimpleNamespace(**{**vars(args), "json": True})
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(run_once, query, args=quiet_args, owner=owner) for query in work_items]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as exc:
                results.append({"error": str(exc), "elapsed_ms": None})

    elapsed_ms = (time.perf_counter() - started) * 1000
    latencies = [item["elapsed_ms"] for item in results if item.get("elapsed_ms") is not None]
    errors = [item for item in results if item.get("error")]
    fallbacks = [
        item
        for item in results
        if (item.get("result") or {}).get("source", "").startswith("llm_query_frontend_failed")
        or (item.get("result") or {}).get("status") == "fallback"
    ]

    summary = {
        "count": len(results),
        "workers": args.workers,
        "elapsed_ms": elapsed_ms,
        "errors": len(errors),
        "fallbacks": len(fallbacks),
        "latency_ms": {
            "min": min(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
            "mean": statistics.mean(latencies) if latencies else None,
            "median": statistics.median(latencies) if latencies else None,
        },
    }

    if args.json:
        print(json.dumps({"summary": summary, "results": _json_safe(results)}, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'=' * 72}")
        print("[Stress Summary]")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return results


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manual query pipeline runner for intent, semantic_query, QueryDSL, and ORM output.",
    )
    parser.add_argument("query", nargs="*", help="Query text. If omitted, sample queries are used.")
    parser.add_argument("--mode", choices=["rag", "search"], default="rag")
    parser.add_argument(
        "--frontend-mode",
        choices=["llm", "passthrough", "off", "disabled"],
        help="Override QUERY_UNDERSTANDING_FRONTEND_MODE for this run.",
    )
    parser.add_argument("--owner-email", help="Save QueryUnderstandingLog as this user.")
    parser.add_argument("--direct-parser", action="store_true", help="Call parse_user_query directly.")
    parser.add_argument("--query-timeout", type=int, help="Override QUERY_UNDERSTANDING_REQUEST_TIMEOUT for this run.")
    parser.add_argument("--max-tokens", type=int, help="Override QUERY_UNDERSTANDING_MAX_TOKENS for this run.")
    parser.add_argument("--query-url", help="Override QUERY_UNDERSTANDING_LLM_URL for this run.")
    parser.add_argument("--query-model", help="Override QUERY_UNDERSTANDING_LLM_MODEL for this run.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--stress", type=int, default=0, help="Run N requests for a lightweight stress test.")
    parser.add_argument("--workers", type=int, default=4, help="Worker threads for --stress.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    owner = _resolve_owner(args.owner_email)
    queries = [" ".join(args.query).strip()] if args.query else DEFAULT_QUERIES
    queries = [query for query in queries if query]
    if not queries:
        raise SystemExit("No query provided.")

    if args.stress > 0:
        run_stress(queries, args=args, owner=owner)
        return 0

    outputs = [run_once(query, args=args, owner=owner) for query in queries]
    if args.json:
        print(json.dumps(_json_safe(outputs), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
