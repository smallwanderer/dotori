# LLM 설치 카탈로그 정책

이 문서는 카탈로그 CLI의 간단한 참조 문서입니다. 설치 정책과 메모리 계산의 기준 문서는 [`llm_installation_policy_and_memory_estimation.md`](./llm_installation_policy_and_memory_estimation.md)입니다.

## 설치 정책

- 운영자는 최초 설치 시 `속도`, `균형`, `품질` 중 하나만 선택합니다.
- 모델 id, Context Length, Concurrency, batch 값은 직접 입력하지 않습니다.
- 오케스트레이터가 하드웨어 프로필과 모델 메타데이터를 사용해 모델과 `serving_profile`을 자동 산출합니다.
- 모델명은 `.env`의 `RAG_LLM_MODEL` 또는 `RAG_VLLM_MODEL`에서 읽지 않습니다.
- 결과는 `data/config/llm_runtime.json`에 저장됩니다.
- llama.cpp 실행 인자는 `data/config/llama_rag.args`에 생성됩니다.

## 명령

```bash
python manage.py llm_model_catalog list
python manage.py llm_model_catalog search qwen
python manage.py llm_model_catalog show qwen2.5-7b-instruct-q4_k_m
python manage.py llm_model_catalog list --priority quality --json-output

python manage.py detect_llm_runtime --priority speed
python manage.py detect_llm_runtime --priority balanced --write
python manage.py detect_llm_runtime --priority quality --write --json-output
python manage.py detect_llm_runtime --cluster-mode --priority balanced --write
python manage.py inspect_llm_runtime
python manage.py inspect_llm_runtime --live --priority quality
python install.py --change-llm
python install.py --change-llm --cluster-mode
```

## Runtime 결정 정책

카탈로그는 모델과 아티팩트 정보만 저장하며 runtime을 고정하지 않는다. 설치 helper가
배포 형태, 아티팩트 포맷, RAM/VRAM을 함께 평가해 runtime을 결정한다.

- cluster mode: vLLM
- AWQ/GPTQ pre-quantized artifact: vLLM
- standalone의 나머지 모델: llama.cpp
- llama.cpp GGUF가 VRAM에 전부 올라가지 않으면 가능한 GPU layer 수를 계산하고 나머지 weight를 RAM에 둔다.
- AWQ/GPTQ vLLM 모델은 VRAM 부족 시 RAM spill로 전환하지 않고 NOFIT으로 표시한다.
- safetensors 모델이 standalone에서 선택됐지만 GGUF artifact가 없으면 변환을 암묵적으로 수행하지 않고 NOFIT으로 표시한다.

resolved runtime은 `llm_runtime.json`의 `target.runtime`과
`target.serving_profile`에 기록된다. llama.cpp는 `llama_rag.args`, vLLM은
`vllm_rag.args`를 생성한다.

초기 설치에서는 `install.py`가 선택된 runtime 서비스만 기동한다. 운영 중 모델을
변경할 때는 호스트에서 `python install.py --change-llm`을 실행한다. 변경 후 반대
runtime 서비스는 중지되고 `rag-worker`가 재시작되어 새 endpoint 설정을 읽는다.

카탈로그 표는 성능 우선순위로 정렬하고 `Safety`를 `safe/danger`, 하드웨어 적합성을 `FIT/RISKY/NOFIT`으로 표시합니다. GPU RAG 런타임은 현재 Compose 오케스트레이션 범위 밖이므로 카탈로그에는 남기되 자동 선택하지 않습니다.
