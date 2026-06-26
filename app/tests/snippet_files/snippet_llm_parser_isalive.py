import os
import sys
import time
from typing import Any

import django
import requests

__test__ = False

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()


model = os.getenv("MANUAL_LLM_MODEL", "google/gemma-4-E4B-it")
system_prompt = (
    "You are a concise retrieval-augmented answer model. "
    "Answer briefly in Korean using plain text only."
)
prompt = "문서 기반 RAG 답변 런타임이 준비되었는지 짧게 설명해줘."
api_url = os.getenv("MANUAL_LLM_API_URL", "http://llama-rag:8080/v1/chat/completions")
health_url = os.getenv("MANUAL_LLM_HEALTH_URL", "http://llama-rag:8080/health")


def build_payload(user_prompt: str) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "top_p": 1.0,
        "max_tokens": 128,
        "stream": False,
        "reasoning_format": "none",
    }


def extract_metrics(result: dict[str, Any]) -> dict[str, Any]:
    usage = result.get("usage", {})
    timings = result.get("timings", {})
    reply = ""
    choices = result.get("choices", [])
    if choices:
        reply = choices[0].get("message", {}).get("content", "")

    return {
        "reply": reply,
        "cached_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "prompt_ms": timings.get("prompt_ms"),
        "predicted_ms": timings.get("predicted_ms"),
        "total_ms": timings.get("prompt_ms", 0) + timings.get("predicted_ms", 0),
    }


def print_metrics(label: str, elapsed: float, result: dict[str, Any]) -> None:
    metrics = extract_metrics(result)
    print(f"\n[{label}]")
    print(f">> wall_time_s: {elapsed:.2f}")
    print(f">> cached_tokens: {metrics['cached_tokens']}")
    print(f">> prompt_tokens: {metrics['prompt_tokens']}")
    print(f">> completion_tokens: {metrics['completion_tokens']}")
    print(f">> prompt_ms: {metrics['prompt_ms']}")
    print(f">> predicted_ms: {metrics['predicted_ms']}")
    print(f">> total_ms: {metrics['total_ms']}")
    print(f">> reply: {metrics['reply']}")


def direct_request(user_prompt: str) -> tuple[float, dict[str, Any]]:
    start_time = time.time()
    response = requests.post(api_url, json=build_payload(user_prompt), timeout=120)
    response.raise_for_status()
    elapsed = time.time() - start_time
    return elapsed, response.json()


def run_external_task_single() -> None:
    print("단일 요청을 보내 llama-rag API 입출력 과정 확인")

    try:
        health_res = requests.get(health_url, timeout=5)
        if health_res.status_code == 200:
            print("[SUCCESS] Health Check 통과 (코드: 200)")
        else:
            print(f"[FAIL] Health Check 실패 (코드: {health_res.status_code})")
    except Exception as exc:
        print(f"[FAIL] Health Check 연결 실패: {exc}")
        return

    print(f">>> POST 요청을 {api_url}로 전송...")
    elapsed, data = direct_request(prompt)

    print("\n" + "#" * 20 + " 외부 API 응답 결과 " + "#" * 20)
    choices = data.get("choices", [])
    if choices:
        print(f"[SUCCESS] 외부 API 응답 완료 (응답 시간: {elapsed:.2f}초)")
        print_metrics("External Single", elapsed, data)
    else:
        print(f"[FAIL] HTTP 200 OK, but invalid response structure: {data}")


def compare_request_paths(user_prompt: str = prompt) -> None:
    print("\n--- External Warm-Cache Comparison ---")
    print(f"질문: {user_prompt}")

    for index in range(1, 3):
        print(f"\n=== Run {index}: external ===")
        try:
            elapsed, result = direct_request(user_prompt)
            print_metrics(f"EXTERNAL RUN {index}", elapsed, result)
        except Exception as exc:
            print(f"[FAIL] external run {index}: {exc}")


if __name__ == "__main__":
    run_external_task_single()
    compare_request_paths()
