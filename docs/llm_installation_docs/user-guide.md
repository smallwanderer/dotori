# LLM 설치 서비스 운영자 가이드

Dotori의 RAG(검색 증강 생성) 기능은 로컬 LLM 서버를 사용합니다.
이 문서는 처음 설치부터 모델 변경, 상태 확인까지 **운영자가 직접 수행하는 모든 작업**을 설명합니다.

---

## 목차

1. [개요 — 어떻게 동작하나요?](#1-개요)
2. [처음 설치](#2-처음-설치)
3. [모델 카탈로그 확인](#3-모델-카탈로그-확인)
4. [LLM 런타임 설정 (핵심 명령어)](#4-llm-런타임-설정)
5. [현재 설정 상태 확인](#5-현재-설정-상태-확인)
6. [모델 변경](#6-모델-변경)
7. [서비스 전환 이해하기](#7-서비스-전환-이해하기)
8. [우선순위(Priority) 선택 기준](#8-우선순위-선택-기준)
9. [트러블슈팅](#9-트러블슈팅)
10. [고급: 클러스터 모드](#10-고급-클러스터-모드)

---

## 1. 개요

Dotori LLM 설치 서비스는 다음 두 가지를 자동으로 처리합니다.

| 자동 처리 | 설명 |
|-----------|------|
| **하드웨어 탐지** | CPU, RAM, GPU, VRAM, 디스크 여유 공간을 측정합니다. |
| **모델 선택** | 탐지된 하드웨어에 맞는 최적 모델과 실행 파라미터를 결정합니다. |

운영자가 선택해야 하는 것은 **우선순위(speed / balanced / quality)** 하나뿐입니다.  
컨텍스트 길이, 동시 처리 수, 배치 크기, 양자화 방식은 모두 자동으로 결정됩니다.

### 지원 런타임

| 런타임 | 사용 조건 | 설명 |
|--------|-----------|------|
| `llama.cpp` | CPU 전용 또는 GPU 부분 오프로드 | GGUF 포맷 모델. RAM이 충분하면 CPU에서 실행. GPU가 있으면 레이어를 VRAM에 올림. |
| `vLLM` | NVIDIA GPU + CUDA | AWQ/GPTQ/safetensors 포맷. GPU 전용. RAM으로 스필 없음. |

---

## 2. 처음 설치

### 방법 A: install.py 마법사 (권장 — Windows/macOS/Linux)

```bash
python install.py
```

화면 안내에 따라 아래 항목을 선택하면 됩니다.

1. **운영 모드** 선택
   - `[1] Full 로컬 AI RAG 모드` — LLM 답변 생성 + 임베딩 + 검색 모두 로컬에서 실행
   - `[2] Hybrid/Search AI 모드` — 임베딩 및 검색만. LLM 미사용.
   - `[3] 기본 모드` — 파일 입출력만.

2. **임베딩 모델** 선택 (모드 1, 2)

3. **쿼리 파서 백엔드** 선택 (모드 1)

4. **RAG 우선순위** 선택 (모드 1)
   - `[1] 속도 우선`, `[2] 균형 (기본값)`, `[3] 품질 우선`

Docker 서비스 구동 후 마법사가 컨테이너 안에서 자동으로 LLM 런타임을 감지하고 설정합니다.

---

### 방법 B: Docker 컨테이너 직접 실행 후 수동 설정

```bash
# 1. Docker 서비스 구동
docker compose -f docker-compose.dev.yml up -d --build

# 2. LLM 런타임 설정 (interactive 마법사)
docker compose -f docker-compose.dev.yml exec app \
  python manage.py detect_llm_runtime --interactive
```

---

## 3. 모델 카탈로그 확인

설치 전에 내 서버에서 어떤 모델을 쓸 수 있는지 미리 확인할 수 있습니다.

### 전체 카탈로그 목록 보기

```bash
docker compose -f docker-compose.dev.yml exec app \
  python manage.py llm_model_catalog list
```

출력 예시:
```
#   Model                    Quant     Size   Device  Logical  Pool Req RAM Req Backend    Speed    Safety  Fit
--- ------------------------ --------- ------ ------- -------- -------- ------- ---------- -------- ------- -------
1   Qwen2.5 7B Instruct      Q4_K_M    7B     CPU     4893MB   5117MB   5117MB  llamacpp-c standard safe    FIT
2   Qwen2.5 7B Instruct AWQ  AWQ       7B     GPU     5632MB   7000MB   0MB     vllm-cuda  fast     safe    NOFIT
```

**컬럼 설명:**

| 컬럼 | 설명 |
|------|------|
| Logical | 모델 가중치 + KV 캐시 + 오버헤드의 논리적 합계 (MB) |
| Pool Req | 실제로 필요한 메모리 풀 (GPU 사용 시 VRAM, CPU 사용 시 RAM) |
| RAM Req | RAM에서 필요한 양 (GPU 모델은 0일 수 있음) |
| Fit | `FIT` (충분), `RISKY` (여유 없음), `NOFIT` (불가) |

### 특정 모델 상세 보기

```bash
docker compose -f docker-compose.dev.yml exec app \
  python manage.py llm_model_catalog show qwen2.5-7b-instruct-q4_k_m
```

### 검색

```bash
docker compose -f docker-compose.dev.yml exec app \
  python manage.py llm_model_catalog search awq
```

### JSON 출력

```bash
docker compose -f docker-compose.dev.yml exec app \
  python manage.py llm_model_catalog list --json-output
```

---

## 4. LLM 런타임 설정

### 자동 선택 (권장)

서버 하드웨어에 맞는 최적 모델과 파라미터를 자동으로 선택하고 저장합니다.

```bash
# balanced 우선순위로 자동 선택 후 저장
docker compose -f docker-compose.dev.yml exec app \
  python manage.py detect_llm_runtime --write

# 우선순위 지정
docker compose -f docker-compose.dev.yml exec app \
  python manage.py detect_llm_runtime --write --priority speed

# 저장하지 않고 미리보기만 (dry-run)
docker compose -f docker-compose.dev.yml exec app \
  python manage.py detect_llm_runtime
```

> [!NOTE]
> `--write` 없이 실행하면 어떤 모델이 선택될지 미리 볼 수 있습니다. 실제 저장은 되지 않습니다.

---

### Interactive 마법사 (단계별 안내)

```bash
docker compose -f docker-compose.dev.yml exec app \
  python manage.py detect_llm_runtime --interactive
```

마법사는 4단계로 진행됩니다:

**Step 1 — 하드웨어 진단 출력**
```
CPU Model: Intel Core i7-12700 (20 Cores)
RAM: Total 32768 MB, Available 24576 MB
GPU: NVIDIA RTX 3080 (10240 MB VRAM, 8192 MB Free)
```

**Step 2 — 운영 정책 선택**
```
1) Speed   2) Balanced   3) Quality
Enter choice (1-3, default: 2):
```

**Step 3 — 모델 순위 및 선택**
```
1) Automatic recommendation (default)
2) Choose from the assessed model catalog
```

자동을 선택하면 최상위 FIT 모델이 자동 선택됩니다.  
수동을 선택하면 카탈로그 목록이 표시되고 번호로 직접 고를 수 있습니다.

> [!IMPORTANT]
> **RISKY** 모델을 수동 선택하면 확인 프롬프트가 나타납니다.  
> `y`를 입력해야만 선택이 확정됩니다. 메모리 여유가 없을 수 있다는 의미입니다.

**Step 4 — 컨테이너 재시작 및 헬스체크**

설정이 저장된 후 런타임 컨테이너를 자동으로 시작하고 30초간 헬스체크를 수행합니다.

---

### 엔드포인트 검증 옵션

```bash
# /health 엔드포인트 확인 후 선택
docker compose -f docker-compose.dev.yml exec app \
  python manage.py detect_llm_runtime --write --check-endpoint

# 실제 채팅 완성 요청으로 스모크 테스트까지 수행
docker compose -f docker-compose.dev.yml exec app \
  python manage.py detect_llm_runtime --write --smoke-test
```

---

## 5. 현재 설정 상태 확인

### 저장된 설정 읽기 (하드웨어 탐지 없이)

```bash
docker compose -f docker-compose.dev.yml exec app \
  python manage.py inspect_llm_runtime
```

출력 예시:
```
Persisted LLM runtime config
path: /app/data/config/llm_runtime.json
exists: True
generated_at: 2026-07-04T12:00:00Z

Configured RAG target
endpoint_name: Qwen2.5 7B Instruct (CPU full)
base_url: http://llama-rag:8080
model: qwen2.5-7b-instruct-q4_k_m
runtime: llama.cpp
priority_preset: balanced
serving_profile: {'context_length': 16384, 'concurrency': 4, ...}
```

### 현재 서버 상태와 함께 확인 (라이브 탐지)

```bash
docker compose -f docker-compose.dev.yml exec app \
  python manage.py inspect_llm_runtime --live
```

> [!TIP]
> 일상적인 상태 확인에는 `inspect_llm_runtime`(하드웨어 탐지 없음)을 사용하세요.  
> `inspect_llm_runtime --live`는 하드웨어 재탐지가 필요한 경우에만 사용합니다.

---

## 6. 모델 변경

런타임 설정을 변경하려면 `detect_llm_runtime`을 다시 실행하면 됩니다.

```bash
# install.py에서 변경 마법사 호출
python install.py --change-llm

# 또는 Docker 컨테이너에서 직접
docker compose -f docker-compose.dev.yml exec app \
  python manage.py detect_llm_runtime --interactive
```

변경 후 자동으로:
1. `data/config/llm_runtime.json` 갱신
2. 선택된 런타임 컨테이너(`llama-rag` 또는 `vllm-rag`) 시작
3. 미사용 런타임 컨테이너 중지
4. `rag-worker` 재시작

---

## 7. 서비스 전환 이해하기

Dotori는 두 RAG 런타임 서비스를 갖습니다. **동시에 하나만 활성화**됩니다.

```
llama-rag   ← llama.cpp 기반 (GGUF 모델, CPU/GPU 혼합)
vllm-rag    ← vLLM 기반  (AWQ/GPTQ 모델, GPU 전용)
```

`llm_runtime.json`에 기록된 `runtime` 값에 따라 서비스가 결정됩니다.

| `runtime` 값 | 활성 컨테이너 |
|-------------|--------------|
| `llama.cpp` | `llama-rag` |
| `vllm` | `vllm-rag` |

### 생성되는 설정 파일

| 파일 | 설명 |
|------|------|
| `data/config/llm_runtime.json` | 런타임 스냅샷. 모델명, URL, 파라미터 전체 포함. |
| `data/config/llama_rag.args` | llama.cpp 서버 실행 인자 (`--ctx-size`, `--parallel` 등) |
| `data/config/vllm_rag.args` | vLLM 서버 실행 인자 (`--model`, `--quantization` 등) |

---

## 8. 우선순위 선택 기준

| 우선순위 | 선택 기준 | 주요 효과 |
|---------|-----------|----------|
| `speed` | 응답 속도 최우선 | 동시 처리 수 증가, 컨텍스트 감소 |
| `balanced` | 속도/품질/메모리 균형 (기본값) | 중간 설정 |
| `quality` | 답변 품질 최우선 | 컨텍스트 최대화, 더 큰 모델 선호 |

> [!TIP]
> **처음 설치라면 `balanced`를 권장합니다.**  
> 하드웨어에 여유가 있는 경우에만 `quality`를 선택하세요.

우선순위는 모델 자체를 바꾸기보다, 선택된 모델의 **실행 파라미터**를 조정하는 역할을 합니다.
동일한 모델을 선택하더라도 우선순위에 따라 컨텍스트 길이와 동시 처리 수가 달라집니다.

---

## 9. 트러블슈팅

### "LLM runtime is not configured" 오류

RAG 질의 시 이 오류가 발생하면 LLM 런타임이 아직 설정되지 않은 것입니다.

```bash
docker compose -f docker-compose.dev.yml exec app \
  python manage.py detect_llm_runtime --interactive
```

---

### 카탈로그 모든 모델이 NOFIT

RAM 또는 VRAM이 부족한 경우입니다. 상세 이유를 확인하세요.

```bash
docker compose -f docker-compose.dev.yml exec app \
  python manage.py llm_model_catalog show <모델-id>
```

`reason` 필드에서 원인을 확인할 수 있습니다.

---

### 헬스체크 실패 후 서비스가 시작되지 않음

```bash
# 런타임 컨테이너 로그 확인
docker compose -f docker-compose.dev.yml logs llama-rag
docker compose -f docker-compose.dev.yml logs vllm-rag

# 수동으로 서비스 재시작
docker compose -f docker-compose.dev.yml restart llama-rag
```

---

### RISKY 모델을 선택해도 되나요?

`RISKY`는 모델 실행에 필요한 메모리가 **사용 가능한 메모리와 거의 동일**하여 25% 안전 여유가 없다는 의미입니다. 시스템이 다른 프로세스로 인해 OOM(메모리 부족)을 겪을 수 있습니다.

- 서버가 RAG 전용이고 다른 부하가 없다면 선택 가능합니다.
- 일반 운영 서버라면 `FIT` 모델을 선택하는 것이 안전합니다.

---

## 10. 고급: 클러스터 모드

여러 GPU에 모델을 분산하여 실행하는 환경에서 사용합니다.

```bash
# 클러스터 모드로 런타임 설정
docker compose -f docker-compose.dev.yml exec app \
  python manage.py detect_llm_runtime --interactive --cluster-mode

# 또는 install.py에서
python install.py --change-llm --cluster-mode
```

클러스터 모드에서는 vLLM의 tensor parallel이 활성화되며, 각 GPU에 모델 레이어를 균등 분산합니다.  
GGUF 포맷은 클러스터 모드에서 선택되지 않습니다.

---

## 참고 파일

| 파일 | 역할 |
|------|------|
| `data/config/llm_runtime.json` | 현재 선택된 런타임 스냅샷 |
| `data/config/llama_rag.args` | llama.cpp 실행 인자 |
| `data/config/vllm_rag.args` | vLLM 실행 인자 |
| `docs/llm_installation_docs/runtime-policy.md` | 런타임 정책 상세 |
| `docs/llm_installation_docs/fit-evaluation.md` | FIT/RISKY/NOFIT 평가 기준 |
| `docs/agent/llm_installation_contract.md` | 구현 계약 (개발자용) |
