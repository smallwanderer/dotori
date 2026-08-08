from openai import OpenAI

OPENAI_KEY = "sk-proj-..."

import os
import sys
import queue
import threading
from typing import Optional

model = "gpt-4.1-mini"

def start_stdin_reader(cmd_queue: queue.Queue[str]) -> None:
    """
    스트리밍 중에도 사용자가 명령을 입력할 수 있게 별도 daemon thread에서 stdin을 읽는다.

    지원 명령:
      /stop              현재 스트림 중단
      /add 추가정보       현재 스트림 중단 후 추가정보를 반영해 새 요청 시작
      Enter              아무 동작 없음, 계속 수신
    """

    def _reader():
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            cmd_queue.put(line.strip())
            
    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t

def stream_once(
    client: OpenAI,
    user_input: str,
    cmd_queue: queue.Queue[str],
    history: list[dict[str, str]] | None = None,
) -> Optional[str]:  
    """
    OpenAI Streaming을 요청.

    return:
        None: 정상 완료 또는 /stop으로 완전 중단
        str: /add 다음에 입력된 새로운 질문 문자열 (이 경우 이 함수가 받은 user_input은 버려짐)
    """

    print("\n--- streaming start ---")
    print("명령: /stop = 중단, /add <추가정보> = 현재 생성 중단 후 새 정보 반영\n")

    accumulated_text = []
    accumulated_messages = []

    stream = client.responses.create(
        model=model,
        input=user_input,
        stream=True
    )

    try:
        for event in stream:
            # event 수집
            accumulated_messages.append(event)

            # 사용자 멍령
            while not cmd_queue.empty():
                cmd = cmd_queue.get_nowait()
                if cmd == "":
                    continue

                elif cmd == "/stop":
                    print("\n--- user interrupted ---")
                    # return/break로 for event in stream 루프 이탈 시 SSE/HTTP 이벤트 종료
                    # 단, 연결을 명시적으로 닫기 위해 finally에서 stream.close()를 호출한다.
                    # with client.responses.stream(...) 구조를 쓰면 context manager가 close를 보장한다.
                    return None

                elif cmd.startswith("/add "):
                    extra = cmd[len("/add ") :].strip()
                    print("\n--- user add detected ---")
                    print(f"extra: {extra}")
                    # 이 함수를 호출한 쪽에서 extra를 반영하여 새 스트림 시작
                    return extra
                
                else:
                    print(f"unknown command: {cmd}")

            # 일반 스트리밍 출력
            if event.type == "response.output_text.delta":
                text = event.delta
                accumulated_text.append(text)

                # 토큰/청크 단위로 화면에 출력
                print(text, end="", flush=True)
            
            elif event.type == "response.created":
                print("\n\n--- response created ---")
            
            elif event.type == "response.in_progress":
                print(f"\n\n--- response progress ---")

            elif event.type == "response.completed":
                print("\n\n--- response completed ---")
                return None

            elif event.type == "response.done":
                print("\n\n--- response done ---")

                return None

            elif event.type == "error":
                print(f"\n\n[server error] {event}")
                return None
            
    finally:
        # openai-python stream 객체 버전에 따라 close/aclose 노출 여부가 다를 수 있어 방어적으로 처리
        close_fn = getattr(stream, "close", None)
        if callable(close_fn):
            close_fn()
        
    return None


def main():
    client = OpenAI(api_key=OPENAI_KEY,)

    cmd_queue: queue.Queue[str] = queue.Queue()
    start_stdin_reader(cmd_queue)

    user_input = """
        저녁 추천좀
    """.strip()

    while True:
        next_input = stream_once(client, user_input, cmd_queue)

        if next_input is None:
            print("\n--- [main] End ---")
            break
        
        # 추가 정보가 들어오면 기존 답변 생성은 버리고,
        # 최신 사용자 입력을 구성해서 새 request를 시작한다.
        user_input = f"""
            기존 요청:
            {user_input}

            추가 정보:
            {next_input}

            위 추가 정보를 반드시 반영해서 처음부터 다시 답변해줘.
        """.strip()        
        
if __name__ == "__main__":
    main()



"""
1. Chat Completion API 구조:

{
  "id": "chatcmpl_xxx",
  "object": "chat.completion",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "오늘 저녁은 김치찌개를 추천합니다."
      },
      "finish_reason": "stop"
    }
  ]
}

2. Responses API 구조:

{
  "id": "resp_xxx",
  "object": "response",
  "status": "completed",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "오늘 저녁은 김치찌개를 추천합니다."
        }
      ]
    }
  ]
}
"""