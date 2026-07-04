# Dotori LLM 설치 정책 및 메모리 산정 설계

## 1. 문서 목적

Dotori는 셀프 호스팅(self-hosted) 서비스이며, RAG 답변 생성용 LLM 런타임은 개별 로그인 사용자가 아니라 **서버 설치 단위**에서 결정되어야 한다.

본 문서는 다음 내용을 하나의 기준으로 정리한다.

- LLM 설치 시점의 모델 선택 정책
- 모델 카탈로그 구조
- 서버 하드웨어 환경 수집 방식
- 서빙 엔진별 실행 파라미터 해석
- LLM 메모리 요구량 계산 방식
- 하드웨어 적합성 평가 기준
- 요청 시점의 런타임 사용 정책
- 향후 서빙 오케스트레이션 확장 방향

---

## 2. 핵심 정책 요약

LLM 모델 선택은 통제된 운영 시점에만 수행한다.

### 2.1 모델 선택이 허용되는 시점

- 최초 설치 과정에서 1회 수행
- 운영자(operator)가 명시적으로 재감지(re-detection)를 요청한 경우에만 재수행

### 2.2 일반 RAG 요청 시 금지되는 동작

일반적인 RAG 요청 발생 시에는 다음 작업을 수행해서는 안 된다.

- 하드웨어 재감지
- Docker 상태 스캔
- 모델 다운로드
- 모델 런타임 자동 구동
- LLM 엔드포인트 재탐색
- 사용자별 모델 동적 선택

요청 시점에는 이전에 저장된 런타임 결정 사항을 읽어서 동일한 대상을 일관되게 사용해야 한다.

### 2.3 모델명 관리 정책

모델명은 `.env`의 다음 값에서 직접 받지 않는다.

```text
RAG_LLM_MODEL
RAG_VLLM_MODEL
```

대신 설치 시점에 모델 카탈로그 데이터를 읽고 자동 선택된 모델 id를 저장한다.

일반 설치 과정에서 운영자에게 노출하는 LLM 선택지는 다음 세 가지 우선순위뿐이다.

- `speed`: 응답 속도 우선
- `balanced`: 속도, 메모리 여유, 품질의 균형
- `quality`: 하드웨어 안전 범위 안에서 품질과 Context 우선

모델 id, Context Length, Concurrency, batch 크기는 사용자 입력으로 받지 않는다. Hardware Profiler와 Resource Estimator가 자동으로 산출하고 `serving_profile`에 기록한다.

---

## 3. 전체 아키텍처

```text
┌──────────────────────────────┐
│ install.py / 운영자 명령       │
└───────────────┬──────────────┘
                │
                ▼
┌──────────────────────────────┐
│ Model Catalog Loader         |
| (loader.py)                  │
│ - defaults.json              │
│ - catalog-data/*.json        │
│ - Pydantic validation        │
└───────────────┬──────────────┘
                │
                ▼
┌──────────────────────────────┐
│ HF Repository Metadata         |
│ - model config                 │
│ - tensor dtype / shape         │
│ - file size                    │
│ - quant 정보                   │
└───────────────┬──────────────┘
                │
                ▼
┌──────────────────────────────┐
│ Hardware Profiler             │
| (runtime_probe.py)            │
│ - CPU RAM                     │
│ - GPU VRAM                    │
│ - Disk                        │
│ - CUDA / Docker / Compose      │
└───────────────┬──────────────┘
                │
                ▼
┌──────────────────────────────┐
│ Engine Capability Resolver    │
│ - llama.cpp params             │
│ - vLLM params                  │
│ - parallel / context / dtype   │
└───────────────┬──────────────┘
                │
                ▼
┌──────────────────────────────┐
│ Resource Estimator / Evaluator │
| (evaluator.py)                 |
│ - Weight memory                │
│ - KV cache memory              │
│ - Compute buffer               │
│ - Runtime overhead             │
│ - FIT / RISKY / NOFIT           │
└───────────────┬──────────────┘
                │
                ▼
┌──────────────────────────────┐
│ data/config/llm_runtime.json │
└──────────────────────────────┘
```

---

## 4. 카탈로그 설계

카탈로그는 설치 전에 확인 가능해야 하며, Docker 내부 명령에 의존하지 않아야 한다.

### 4.1 디렉터리 구조

```text
app/document_ai/llm-installation-helper/catalog/
├── defaults.json
├── models.py
├── loader.py
├── evaluator.py
├── services.py
└── catalog-data/
    ├── qwen2.5-7b-instruct-q4_k_m.json
    ├── qwen2.5-7b-instruct-awq.json
    └── qwen2.5-7b-instruct-bf16.json
```

### 4.2 파일별 역할

| 파일 | 역할 |
|---|---|
| `defaults.json` | 공통 기본값, schema version, 기본 파라미터 정의 |
| `models.py` | Pydantic 도메인 모델 정의 |
| `loader.py` | JSON 설정 병합 및 Pydantic 로드 라이프사이클 담당 |
| `evaluator.py` | 하드웨어 사양 매칭 및 적합성 검증 |
| `services.py` | ORM/Celery 연동, 선호 모델, 다운로드 관련 비즈니스 로직 |
| `catalog-data/*.json` | 모델별 개별 설정 파일 |

---

## 5. HF Repository Metadata

HF Repository 영역은 모델의 공식 메타데이터와 파일 정보를 수집하는 책임을 가진다.

### 5.1 수집 대상

- `model_id`
- `revision` / `commit_sha`
- `file_list`
- `file_size`
- `architecture`
- `hidden_size`
- `layer_num`
- `attention_heads`
- `kv_heads`
- `context_length`
  - `max_position_embeddings`
  - `RoPE scaling`
- `tensor dtype`
- `tensor shape`
- `parameter count by dtype`
- `GGUF quant`
- `license`

### 5.2 목적

HF Repository metadata는 모델 자체가 요구하는 기본 메모리 사용량을 계산하기 위한 원천 데이터이다.

특히 GGUF 모델은 tensor별 quant dtype이 다를 수 있으므로, 전체 파라미터 수만으로 weight memory를 계산하면 오차가 발생할 수 있다.

---

## 6. Engine Capability Resolver

Engine Capability Resolver는 서빙 엔진별 실행 파라미터를 해석하고, 메모리 계산에 반영한다.

지원 대상 엔진은 우선 llama.cpp를 기준으로 한다.

- `llama.cpp`

---

# 6.1 llama.cpp

`llama.cpp`는 CPU 또는 단일/멀티 GPU 환경에서 GGUF 모델을 실행하는 주요 런타임이다.

## 6.1.1 주요 파라미터

### `--ctx-size`

LLM이 한 번에 처리할 수 있는 컨텍스트 윈도우 크기이다.

컨텍스트 길이는 KV cache 메모리에 선형적으로 영향을 준다.

```text
KV cache bytes =
context_size
* layers
* kv_heads
* head_dim
* 2
* dtype_bytes
* active_slots_or_sequences
```

여기서 `2`는 Key와 Value를 의미한다.

---

### `--batch-size`

모델이 한 번에 연산하는 최대 토큰 개수이다.

프롬프트 처리 속도에 큰 영향을 준다.

예를 들어 기본값이 `512`인 경우, 1000토큰 프롬프트는 다음과 같이 나뉘어 처리된다.

```text
1000 tokens → 512 tokens + 488 tokens
```

---

### `--ubatch-size`

`batch-size`를 다시 나누어 실제 연산 장치에 전달하는 micro batch 단위이다.

전체 batch를 한 번에 GPU에 올리면 OOM이 발생할 수 있으므로, 실제 연산 장치에는 `ubatch-size` 단위로 나누어 전달한다.

---

### `--gpu-layers`

모델의 몇 개 레이어를 GPU에 올릴지 결정하는 옵션이다.

일반적으로 전체 레이어를 GPU에 올리고 싶은 경우 다음과 같은 값을 사용한다.

```bash
--gpu-layers -1
```

또는 충분히 큰 값을 지정할 수 있다.

```bash
--gpu-layers 99
```

메모리 계산은 다음과 같이 나뉜다.

```text
gpu_weight_memory = sum(layer_assigned_to_gpu)

cpu_weight_memory = total_weight_memory - gpu_weight_memory
```

주의할 점은 GGUF 모델의 경우 tensor별 quant dtype이 다를 수 있어, GPU weight memory 계산이 단순하지 않다는 것이다.

---

### `--split-mode`

GPU가 여러 장 있을 때 연산 분할 방식을 결정한다.

주요 모드는 다음과 같다.

- `layer`
- `row`

`row` 모드는 MoE 모델 또는 고대역폭 GPU 환경에서 성능과 생성 속도 측면에서 유리할 수 있다.

---

### `--cache-type-k`

Key cache의 양자화 수준을 결정한다.

예시는 다음과 같다.

- `f16`
- `q8_0`
- `q4_0`

Key cache는 attention 계산에 직접적인 영향을 주므로, 양자화 수준에 따라 품질과 성능에 영향이 발생할 수 있다.

---

### `--cache-type-v`

Value cache의 양자화 수준을 결정한다.

예시는 다음과 같다.

- `f16`
- `q8_0`
- `q4_0`

Value cache 양자화는 Key cache보다 성능 영향이 상대적으로 적은 편이다.

긴 컨텍스트를 사용하는 경우 VRAM 사용량을 줄이는 데 효과적이다.

---

### `--parallel`

동시에 처리할 병렬 sequence 수를 의미한다.

현재 Dotori RAG worker가 단일 동시성으로 동작하므로 초기 계산 정책에서는 다음과 같이 자동 결정한다.

```bash
--parallel 1
```

이 값은 사용자 설정이 아니다. 향후 RAG worker 자체가 확장될 때 오케스트레이터가 worker concurrency와 메모리 허용량의 최솟값으로 다시 계산한다.

---

### `--cont-batching`

Continuous batching 관련 옵션이다.

병렬 요청 처리와 관련된 기능이지만, 초기 계산 구조에서는 비활성화하는 방향으로 둔다.

---

## 6.1.2 llama.cpp 메모리 계산 포인트

### GPU weight memory

```text
gpu_weight_memory = sum(layer_assigned_to_gpu)
```

### CPU weight memory

```text
cpu_weight_memory = total_weight_memory - gpu_weight_memory
```

### KV cache memory

```text
kv_cache_memory =
context_size
* layers
* kv_heads
* head_dim
* 2
* dtype_bytes
* active_slots_or_sequences
```

---

# 6.2 vLLM

`vLLM`은 서버형 병렬 처리, 높은 throughput, GPU 기반 KV cache 관리가 중요한 런타임이다.

## 6.2.1 주요 파라미터

### `max_model_len`

모델이 처리할 최대 컨텍스트 길이이다.

llama.cpp의 `--ctx-size`와 유사하게 KV cache 메모리 사용량에 직접적인 영향을 준다.

---

### `max_num_seqs`

동시에 처리할 수 있는 최대 sequence 수이다.

동시 요청 수와 관련되며 KV cache 메모리 사용량에 영향을 준다.

---

### `kv_cache_dtype`

KV cache의 dtype을 결정한다.

예시는 다음과 같다.

- `auto`
- `fp16`
- `bf16`
- `fp8`

KV cache dtype이 작아질수록 메모리 사용량은 감소하지만, 모델 품질이나 성능에 영향을 줄 수 있다.

---

### `gpu_memory_utilization`

vLLM이 GPU VRAM 중 얼마만큼을 사용할 수 있는지 결정하는 비율이다.

예시는 다음과 같다.

```text
gpu_memory_utilization = 0.9
```

이는 전체 VRAM 중 약 90%까지 vLLM이 사용할 수 있음을 의미한다.

---

### `max_num_batched_tokens`

한 번에 batch로 묶어 처리할 수 있는 최대 토큰 수이다.

프롬프트 처리량과 throughput에 영향을 준다.

---

### `tensor_parallel_size`

모델 tensor를 여러 GPU에 나누어 배치하는 tensor parallel 크기이다.

GPU가 여러 장일 때 모델 weight를 분산하여 올릴 수 있다.

---

### `pipeline_parallel_size`

모델 layer를 여러 GPU에 pipeline 형태로 나누어 배치하는 pipeline parallel 크기이다.

대형 모델을 여러 GPU에 나누어 실행할 때 사용한다.

---

## 7. Hardware Profiler

Hardware Profiler는 서버의 하드웨어 및 런타임 환경을 수집한다.

현재 런타임 probe는 서버/컨테이너 환경 데이터만 감지한다.

브라우저, 클라이언트 PC, 개인 정보, 문서 내용, 위치 또는 사용 정보는 수집하지 않는다.

### 7.1 감지 필드

- `cpu_count`
  - `os.cpu_count()`로부터 획득
- `cpu_model`
  - `/proc/cpuinfo` 또는 Windows `wmic cpu`에서 획득한 CPU 모델명
- `cpu_features`
  - `/proc/cpuinfo`의 `flags` 또는 `features`로부터 획득한 지원 명령어 셋 리스트
- `ram_mb`
  - 시스템 총 RAM 용량 (`/proc/meminfo`의 `MemTotal` 또는 Windows API 호출)
- `ram_available_mb`
  - 시스템 사용 가능한 RAM 용량 (`/proc/meminfo`의 `MemAvailable`/`MemFree` 또는 Windows API 호출)
- `disk_free_mb` / `disk_total_mb`
  - `/data` 경로가 존재하면 해당 파일시스템 기준, 없으면 현재 작업 디렉토리 기준 파일시스템의 여유 공간 및 총 용량
- `container_memory_limit_mb`
  - cgroup memory limit이 설정된 경우 컨테이너 메모리 제한 제한값
- `has_gpu`
  - GPU 장치 정보가 감지되면 `true`
- `gpu_count`
  - 감지된 전체 GPU 개수
- `gpu_name`
  - 첫 번째 GPU의 공식 모델명
- `gpu_vram_mb` / `gpu_vram_free_mb`
  - 모든 GPU의 총 VRAM 용량 및 사용 가능한 VRAM 용량
- `gpu_vram_list` / `gpu_vram_free_list`
  - 장착된 모든 GPU별 총 VRAM 및 실시간 남은 VRAM 리스트
- `gpu_compute_cap`
  - 첫 번째 GPU의 연산 역량 (Compute Capability, 예: `"8.9"`)
- `gpu_mem_bandwidth_gb_s`
  - 첫 번째 GPU의 메모리 대역폭 (GB/s)
- `cuda_available`
  - 백엔드가 `"nvidia"` 또는 `"torch"`인 경우 `true`
- `cuda_driver_version`
  - NVIDIA 드라이버 버전 (NVIDIA SMI 쿼리 결과)
- `driver_supported_cuda_version`
  - NVIDIA 드라이버가 지원하는 호환 CUDA 최대 버전 (NVIDIA SMI 쿼리 결과)
  - NVIDIA driver compatibility version
- `docker_available`
  - `docker --version` 실행 가능 여부
- `docker_compose_available`
  - `docker compose version` 실행 가능 여부
- `docker_services`
  - `docker compose ps --format json`으로 획득한 서비스 상태 리스트
- `platform`
  - `platform.platform()`으로부터 획득한 OS 플랫폼 식별 정보
- `gpu_probe_result`
  - 감지된 세부 GPU 정보를 담고 있는 `GpuProbeResult` 구조체. 포함되는 세부 속성은 다음과 같음:
    - `backend`: 감지 방식 (`"nvidia"`, `"rocm"`, `"torch"`, `"metal"`, `"none"`)
    - `gpu_count`: 감지된 총 GPU 수
    - `devices`: 각 GPU 장치별 `GpuDeviceInfo` 목록
      - `index`: GPU 인덱스
      - `name`: 장치 모델명
      - `total_vram_mb` / `free_vram_mb`: 총 VRAM 및 실시간 남은 VRAM
      - `compute_capability`: 연산 성능 규격 (`"gfx"`, `"metal"`, 혹은 CUDA CC 등)
      - `driver_version`: OS/물리 드라이버 버전 (NVIDIA `nvidia-smi` 및 AMD `amdgpu` 커널 드라이버에서 추출한 버전 등)
      - `uuid`: GPU 고유 UUID (지원 시)
      - `pci_bus_id` / `pci_device_id`: PCI 버스 및 장치 식별자 (NVIDIA 지원 시)
      - `memory_clock_mhz`: 최대 메모리 클럭 속도
      - `memory_bus_width_bits`: 메모리 버스 폭 (bits)
      - `bandwidth_gb_s`: 메모리 대역폭 (GB/s)
      - `bandwidth_source`: 대역폭 데이터 출처
          - "known_gpu_table": 미리 정의된 GPU 테이블에서 조회한 값
          - "nvidia_smi_estimate": NVIDIA SMI에서 추정한 값
          - "unknown": 알 수 없는 값
      - `bandwidth_confidence`: 데이터 신뢰 수준
          - "high": "known_gpu_table"을 사용한 경우
          - "medium": "nvidia_smi_estimate"을 사용한 경우
          - "low": "unknown"을 사용한 경우
---

## 8. Resource Estimator

Resource Estimator는 모델 메타데이터, 엔진 설정, 하드웨어 정보를 종합하여 필요한 리소스를 계산한다.

---

## 8.1 모델 weight memory

모델 파라미터가 차지하는 메모리이다.

```text
weight_memory = sum(parameter_count_by_dtype * dtype_bytes)
```

GGUF 모델의 경우 tensor별 quant dtype이 다를 수 있으므로, 가능하면 tensor별 dtype/shape 기반으로 계산해야 한다.

---

## 8.2 KV cache memory

KV cache memory는 다음 요소에 의해 결정된다.

- context size
- layer 수
- KV head 수
- head dimension
- Key/Value 양쪽 캐시
- dtype bytes
- active sequence 수

```text
kv_cache_memory =
context_size
* layers
* kv_heads
* head_dim
* 2
* dtype_bytes
* active_slots_or_sequences
```

metadata가 부족한 경우에는 다음과 같이 계산된다.
```text
kv_cache_memory = 
0.000008
* context_size
* params_b
```

---

## 8.3 Compute buffer memory

Compute buffer memory는 다음 요소에 따라 달라진다.

- batch size
- ubatch size
- backend
- attention 구현 방식
- GPU backend
- quant 방식
- tensor parallel / pipeline parallel 여부

정확한 값은 엔진 구현에 따라 달라질 수 있으므로, 초기에는 추정값 또는 안전 margin을 적용한다.

---

## 8.4 CPU memory

CPU RAM 사용량은 다음과 같이 계산한다.

```text
cpu_memory =
cpu_weight_memory
+ tokenizer_memory
+ metadata_memory
+ runtime_overhead
```

---

## 8.5 GPU memory

GPU VRAM 사용량은 다음과 같이 계산한다.

```text
gpu_memory =
gpu_weight_memory
+ kv_cache_memory
+ compute_buffer_memory
+ runtime_overhead
```

---

## 8.6 전체 메모리 계산 요약

```text
Total Required Memory
=
Model Weight Memory
+ KV Cache Memory
+ Compute Buffer Memory
+ Runtime Overhead
```

GPU 기준:

```text
Required VRAM
=
GPU Weight Memory
+ KV Cache Memory
+ GPU Compute Buffer
+ GPU Runtime Overhead
```

CPU 기준:

```text
Required RAM
=
CPU Weight Memory
+ Tokenizer / Metadata
+ CPU Runtime Overhead
```

---

## 9. Hardware Fit Evaluation

하드웨어 적합성은 모델의 요구 메모리와 감지된 하드웨어 리소스를 비교하여 분류한다.

분류 결과는 다음 세 가지이다.

- `FIT`
- `RISKY`
- `NOFIT`

---

## 9.1 CPU / llama.cpp 런타임

CPU 기반 llama.cpp 런타임은 RAM 요구량을 기준으로 평가한다.

```text
Estimated_RAM_MB =
weight_memory_mb
+ kv_cache_mb
+ compute_buffer_mb
+ runtime_overhead_mb
```

OS 및 Django 웹 서비스의 가용 영역 확보를 위해 25%의 시스템 RAM 여유분을 검사한다.

| 조건 | 판정 | 의미 |
|---|---|---|
| `Estimated_RAM * 1.25 <= Total_System_RAM` | `FIT` | 권장 |
| `Estimated_RAM <= Total_System_RAM < Estimated_RAM * 1.25` | `RISKY` | 경고와 함께 선택 허용 |
| `Total_System_RAM < Estimated_RAM` | `NOFIT` | 실행 불가 또는 필터링 |

---

## 9.2 GPU / vLLM 런타임

GPU 기반 vLLM 런타임은 VRAM 요구량을 기준으로 평가한다.

```text
Required_VRAM_MB =
(weight_memory_mb + cuda_context_mb + compute_buffer_mb)
/
gpu_memory_utilization
```

GPU VRAM OOM으로 인한 컨테이너 크래시를 예방하기 위해 15% ~ 25%의 VRAM 여유분을 검사한다.

| 조건 | 판정 | 의미 |
|---|---|---|
| `Required_VRAM * 1.25 <= Total_GPU_VRAM` | `FIT` | 권장 |
| `Required_VRAM <= Total_GPU_VRAM < Required_VRAM * 1.25` | `RISKY` | 경고와 함께 선택 허용 |
| `Total_GPU_VRAM < Required_VRAM` | `NOFIT` | 실행 불가 |
| CUDA 사용 불가 | `NOFIT` | GPU 런타임 실행 불가 |

---

## 10. 설치 동작

`install.py`는 최초 Docker 구동이 완료된 후, 운영자가 **Full 로컬 AI RAG 모드**를 선택했을 때만 LLM 감지 명령을 실행한다.

설치 wizard는 모델 목록이나 Context/Concurrency 값을 묻지 않고 운영 우선순위만 표시한다.

```text
1. 속도 우선
2. 균형
3. 품질 우선
```

선택값은 Docker 구동 후 다음 명령에 전달된다.

```bash
python manage.py detect_llm_runtime --write --priority <speed|balanced|quality>
```

카탈로그 조회는 설치 선택과 분리된 운영자 명령이다.

따라서 Docker를 띄우기 전에도 다음 명령으로 카탈로그를 확인할 수 있어야 한다.

```bash
python install.py --list-llm-models
```

### 10.1 설치 wizard 표시 컬럼

Full 로컬 AI RAG 모드에서는 catalog 설정을 읽어 다음 컬럼을 표시한다.

```text
Model | Quant | Size | Device | Min Mem | Rec Mem | RAM | Backend | Speed | Safety | Fit
```

### 10.2 컬럼 의미

| 컬럼 | 의미 |
|---|---|
| `Model` | 사용자에게 보여줄 모델명 |
| `Quant` | GGUF quant 또는 GPU dtype 계열 |
| `Size` | 모델 크기 계열 |
| `Device` | CPU 또는 GPU |
| `Min Mem` | 모델 실행에 필요한 최소 주 메모리 또는 VRAM |
| `Rec Mem` | 권장 주 메모리 또는 VRAM |
| `RAM` | 권장 시스템 RAM |
| `Backend` | `llama.cpp`, `vLLM` 등 런타임 백엔드 |
| `Speed` | 상대 속도 힌트 |
| `Safety` | 카탈로그 정책 기준 `safe` 또는 `danger` |
| `Fit` | 현재 하드웨어 기준 `FIT`, `RISKY`, `NOFIT` |

### 10.3 자동 Serving Profile

- `concurrency`: 현재 RAG worker 구조에 맞춰 `1`
- `context_length`: 기본 `4096`, 품질 우선은 `8192`를 검토
- 품질 우선 8K가 25% 메모리 안전 마진을 충족하지 못하면 4K로 자동 강등
- `batch_size`, `ubatch_size`, `max_num_seqs`, `max_num_batched_tokens`는 엔진 메타데이터와 우선순위로 자동 산출
- 생성 결과는 `data/config/llm_runtime.json`의 `target.serving_profile`에 저장
- llama.cpp 인자는 `data/config/llama_rag.args`에 생성하고 `llama-rag` 재시작 시 적용

---

## 11. 현재 런타임 설정 흐름

현재 구현은 서버 전역에 설정된 RAG 대상을 다음 경로에 저장한다.

```text
data/config/llm_runtime.json
```

### 11.1 기본 감지 및 저장

```bash
python manage.py detect_llm_runtime --write --priority balanced
```

### 11.2 우선순위별 재감지

```bash
python manage.py detect_llm_runtime --write --priority speed
python manage.py detect_llm_runtime --write --priority quality
```

### 11.3 엔드포인트 검증 포함

```bash
python manage.py detect_llm_runtime --write --check-endpoint
```

### 11.4 저장된 설정 확인

하드웨어를 재감지하지 않고 저장된 현재 설정을 확인한다.

```bash
python manage.py inspect_llm_runtime
```

### 11.5 live 검증 확인

```bash
python manage.py inspect_llm_runtime --live
```

---

## 12. 선택적 엔드포인트 검증

운영자가 명시적으로 요청한 경우에만 엔드포인트 검증을 수행한다.

### 12.1 Health check

```text
GET {base_url}/health
```

확인 항목:

- HTTP 상태 코드
- 응답 시간

### 12.2 Model list check

```text
GET {base_url}/v1/models
```

확인 항목:

- HTTP 상태 코드
- 응답 시간
- endpoint가 반환한 모델 목록 일부
- 선택한 모델이 모델 목록에 존재하는지 여부

### 12.3 Smoke test

```text
POST {base_url}/v1/chat/completions
```

확인 항목:

- 선택한 모델명으로 짧은 응답 생성 가능 여부
- HTTP 상태 코드
- 응답 시간
- 간단한 실패 메시지

---

## 13. 요청 시점 동작

RAG 작업(job)이 생성될 때 이미 구성된 서버의 대상 정보를 스냅샷으로 기록해야 한다.

### 13.1 RAGJob snapshot 필드

- `RAGJob.llm_endpoint_name`
- `RAGJob.llm_base_url`
- `RAGJob.llm_model`

### 13.2 설정 파일 누락 또는 invalid 상태

`data/config/llm_runtime.json` 파일이 누락되었거나 유효하지 않은 경우, RAG 처리 경로는 내장 catalog 데이터의 기본 CPU 모델을 사용해야 한다.

단, 이 경우에도 실시간 하드웨어 감지나 Docker 상태 스캔을 수행해서는 안 된다.

### 13.3 목적

이 정책의 목적은 다음과 같다.

- 요청 latency 예측 가능성 유지
- 일반 사용자 workflow에 하드웨어 탐색 노출 방지
- RAG 요청마다 모델 선택 결과가 흔들리는 문제 방지
- 설치 시점 결정과 요청 시점 실행을 명확히 분리

---

## 14. 현재 선택 범위

1단계 라우터는 보수적인 결정을 내린다.

- 모델 카탈로그는 runtime별 중복 엔트리 대신 모델/아티팩트 정보를 저장한다.
- cluster mode 또는 AWQ/GPTQ pre-quantized artifact는 `vLLM`으로 결정한다.
- standalone의 나머지 모델은 `llama.cpp`로 결정한다.
- GGUF weight가 VRAM보다 크면 `n_gpu_layers`를 계산해 일부 layer만 GPU에 두고 나머지는 RAM에 둔다.
- AWQ/GPTQ는 VRAM 부족 시 llama.cpp/RAM spill로 우회하지 않고 NOFIT으로 판정한다.
- `llama-rag`와 `vllm-rag` 중 선택된 서비스만 기동하고 반대 서비스는 중지한다.
- 적합한 후보가 없는 경우 built-in catalog fallback으로 대체한다.
- 선택적인 엔드포인트 확인을 통해 응답이 없거나 선택 모델이 목록에 없는 엔드포인트는 건너뛸 수 있다.
- 선택적인 스모크 테스트를 통해 실제 chat completion 요청이 실패하는 후보를 건너뛸 수 있다.

이 정도 수준은 기본 셀프 호스팅 설치에는 충분하다.

선택된 llama.cpp 모델과 생성 실행값은 별도 args 파일로 기록되어 컨테이너 재시작 시 실제 서버 명령에 반영된다.

---

## 15. 2단계 구현 결과

2단계의 목표는 자동화된 런타임 오케스트레이션이 아니라 **자동 검증(automatic validation)**이다.

현재 2단계 구현은 저장된 설치 결정의 신뢰성을 높이기 위해 다음 항목을 추가한다.

- 디스크 여유 공간 감지
- Docker / Compose 사용 가능 여부 확인
- 가능한 경우 Docker 서비스 상태 확인
- 컨테이너 메모리 제한 감지
- 사용 가능한 경우 CUDA 드라이버 및 런타임 버전 감지
- `/health` 및 `/v1/models`를 활용한 엔드포인트 검증
- 선택한 모델에 대한 chat-completion smoke test
- 승인된 후보군과 거부된 후보군에 대한 상세 진단 기록

### 15.1 2단계 명령 옵션

```bash
python manage.py detect_llm_runtime --write
python manage.py detect_llm_runtime --write --priority speed
python manage.py detect_llm_runtime --write --priority balanced
python manage.py detect_llm_runtime --write --priority quality
python manage.py detect_llm_runtime --write --check-endpoint
python manage.py detect_llm_runtime --write --check-endpoint --smoke-test
python manage.py detect_llm_runtime --list-models
python manage.py detect_llm_runtime --search qwen --json-output
python manage.py detect_llm_runtime --show qwen2.5-7b-instruct-q4_k_m
python manage.py detect_llm_runtime --cluster-mode --write
python install.py --change-llm
python manage.py inspect_llm_runtime
python manage.py inspect_llm_runtime --live --check-endpoint
python manage.py inspect_llm_runtime --live --check-endpoint --smoke-test
```

### 15.2 저장 설정 파일의 최상위 구조

저장되는 설정 파일에는 운영자가 다음 사항을 파악할 수 있도록 충분한 진단 정보가 포함되어야 한다.

- 어떤 모델이 선택되었는지
- 왜 그 모델이 선택되었는지
- 어떤 후보 모델들이 거부되었는지
- fallback 설정이 사용되었는지 여부
- 엔드포인트 및 모델 검증이 통과되었는지 여부

최상위 필드는 다음과 같다.

```text
target
profile
catalog
diagnostics
```

| 필드 | 의미 |
|---|---|
| `target` | RAG가 사용할 endpoint/model/runtime, priority 및 자동 serving profile |
| `profile` | 감지된 서버/컨테이너 환경 정보 |
| `catalog` | 평가 대상 모델 후보 목록 |
| `diagnostics` | 후보별 선택/탈락 사유와 endpoint/smoke 결과 |

---

## 16. 현재 오케스트레이션 범위와 비목표

현재 구현은 2단계 자동 검증 위에 다음 최소 오케스트레이션을 추가한다.

- 우선순위와 하드웨어 기반 모델 자동 선택
- Context, concurrency, batch 관련 실행값 자동 산출
- `llm_runtime.json`과 `llama_rag.args` 생성
- 최초 설치 시 생성 인자를 적용하기 위한 `llama-rag` 재시작
- 컨테이너 시작 시 생성 args를 읽는 entrypoint

현재 단계에서는 다음 기능을 추가하지 않는다.

- 유휴(idle) 상태일 때 런타임 종료
- GPU 할당/스케줄링
- GPU RAG 런타임 자동 기동
- vLLM GPU 종류별 quantization/kernel 호환성 검증 강화
- 사용자별 모델 정책 적용
- 과금/할당량(quota) 기반 모델 라우팅

이 기능들은 향후 서빙 오케스트레이션 단계에 속한다.

---

## 17. 2단계 검증 결과

현재 2단계 구현은 다음 명령으로 검증했다.

```bash
python3 -m compileall app/document_ai/llm_installation_helper app/document_ai/management/commands
python3 install.py --list-llm-models
python3 install.py --search-llm qwen --json-output
python3 install.py --show-llm qwen2.5-7b-instruct-q4_k_m
docker.exe compose -f docker-compose.dev.yml exec app python -m pytest tests/test_llm_router.py tests/test_rag_flow.py
docker.exe compose -f docker-compose.dev.yml exec app python manage.py llm_model_catalog list
docker.exe compose -f docker-compose.dev.yml exec app python manage.py llm_model_catalog search qwen --json-output
docker.exe compose -f docker-compose.dev.yml exec app env LLM_RUNTIME_CONFIG_PATH=/tmp/llm_runtime_priority.json python manage.py detect_llm_runtime --priority quality --write --json-output
docker.exe compose -f docker-compose.dev.yml exec app env LLM_RUNTIME_CONFIG_PATH=/tmp/llm_runtime_stage2_final.json python manage.py detect_llm_runtime --write
docker.exe compose -f docker-compose.dev.yml exec app env LLM_RUNTIME_CONFIG_PATH=/tmp/llm_runtime_stage2_final.json python manage.py inspect_llm_runtime
docker.exe compose -f docker-compose.dev.yml exec app env LLM_RUNTIME_CONFIG_PATH=/tmp/llm_runtime_stage2_checked.json python manage.py detect_llm_runtime --write --check-endpoint
```

검증 결과:

- `tests/test_llm_router.py` 단독 실행: `11 passed`
- `tests/test_llm_router.py tests/test_rag_flow.py`: `35 passed`
- `install.py` 기반 list/search/show 명령: Docker 없이 실행 확인
- 품질 우선 8K의 메모리 안전 마진 미충족 시 4K 자동 강등 확인
- `llama_rag.args` 생성 및 Compose 구성 검증 확인
- `llm_model_catalog` 기반 list/search/show/json 명령: 컨테이너 내부 실행 확인
- `detect_llm_runtime --priority quality --write --json-output`: JSON만 출력하고 자동 선택 결과 저장 확인
- 기본 `detect_llm_runtime --write`: `Server auto` target 저장 성공
- `inspect_llm_runtime`: 저장된 config와 후보 diagnostics 표시 확인
- `detect_llm_runtime --write --check-endpoint`: 모델 목록 불일치 시 후보 탈락 diagnostics와 fallback 저장 확인

---

## 18. 최종 결과 구조 예시

Resource Estimator와 Runtime Resolver가 결합된 결과는 다음과 같은 형태로 표현할 수 있다.

```json
{
  "target": {
    "endpoint_name": "local-llamacpp",
    "base_url": "http://localhost:8001",
    "model": "qwen2.5-7b-instruct-q4_k_m",
    "backend": "llama.cpp",
    "device": "cpu",
    "validation": {
      "endpoint_checked": false,
      "smoke_tested": false,
      "passed": null
    }
  },
  "profile": {
    "cpu_count": 8,
    "ram_mb": 32768,
    "disk_free_mb": 512000,
    "has_gpu": false,
    "gpu_name": null,
    "gpu_vram_mb": 0,
    "cuda_available": false,
    "docker_available": true,
    "docker_compose_available": true,
    "platform": "Linux"
  },
  "catalog": {
    "model_id": "qwen2.5-7b-instruct-q4_k_m",
    "hf_model_id": "Qwen/Qwen2.5-7B-Instruct",
    "quant": "Q4_K_M",
    "size": "7B",
    "license": "apache-2.0"
  },
  "memory": {
    "total_weight_memory_mb": 4600,
    "gpu_weight_memory_mb": 0,
    "cpu_weight_memory_mb": 4600,
    "kv_cache_memory_mb": 2048,
    "compute_buffer_memory_mb": 1024,
    "runtime_overhead_mb": 512,
    "estimated_ram_mb": 8184,
    "estimated_vram_mb": 0
  },
  "result": {
    "fit": "FIT",
    "margin_ram_mb": 24584,
    "margin_vram_mb": null,
    "recommendation": "현재 설정으로 실행 가능"
  },
  "diagnostics": {
    "accepted": [
      "RAM satisfies 25% safety margin"
    ],
    "rejected": [],
    "fallback_used": false
  }
}
```

---

## 19. 결과 정렬 기준

모델 후보들은 다음 기준으로 정렬할 수 있다.

1. 실행 가능 여부
2. `FIT` 여부
3. 예상 VRAM 사용량
4. 예상 CPU RAM 사용량
5. 예상 disk 사용량
6. 최대 지원 context length
7. 양자화 수준
8. 엔진 호환성
9. 상대 속도
10. fallback 여부

---

## 20. 향후 확장 포인트

향후 서빙 오케스트레이션 단계에서는 다음 기능을 추가할 수 있다.

- GGUF tensor별 dtype 기반 weight memory 계산
- HF safetensors metadata 기반 dtype/shape 분석
- vLLM block manager 기반 KV cache 계산
- MoE 모델의 active parameter 계산
- tensor parallel / pipeline parallel 반영
- multi-GPU split 전략별 VRAM 분배 계산
- CUDA / ROCm / Metal backend별 compute buffer 차이 반영
- 실제 실행 로그 기반 보정값 적용
- GPU/vLLM 모델별 실행 서비스 및 명령 자동 생성
- GPU 런타임 시작/중지
- 유휴 상태 런타임 종료
- 용량 추적
- GPU/CPU 런타임 임대 관리

---

## 21. 설계 경계

향후 서빙 매니저가 추가되더라도 다음 책임은 분리되어야 한다.

- 엔드포인트/모델 선택
- 설치 시점 감지
- 하드웨어 적합성 평가
- RAG 작업 스냅샷 생성
- 실제 모델 런타임 오케스트레이션

현재 설치 정책은 이 경계를 명확히 구분하는 것을 목표로 한다.
