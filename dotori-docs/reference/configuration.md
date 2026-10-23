# 설정 항목

> 이전: [설치 가이드](../guides/install/installation-guide.md)

이 문서는 `.env`의 항목과 `install.py`의 명령을 한곳에 모아 조회할 수 있게 합니다. 기준은 저장소의 `.env.example`이며, 아래에 없는 항목은 코드가 읽지 않거나 바꿀 필요가 없는 값입니다. 항목을 바꾸는 절차는 [설정 변경 적용](../guides/operate/applying-changes.md)과 [운영 가이드](../guides/operate/operation-guide.md)가 다룹니다.

`.env`를 고친 뒤에는 `python install.py --restart`로 서비스를 다시 시작해야 반영됩니다. 이 규칙의 예외는 항목 표에 적었습니다.

## 설치가 만드는 값

`python install.py`가 처음 `.env`를 만들 때 채우거나, 서비스 이름처럼 환경에 맞춰 고정되는 값입니다. 운영 중에 바꾸지 않습니다.

| 항목 | 설명 |
| --- | --- |
| `DJANGO_SECRET_KEY` | 세션 서명에 쓰는 비밀 값입니다. 설치 때 무작위로 만들어집니다. `.env.example`을 직접 복사해 쓴다면 고유한 값으로 바꿔야 합니다 |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | 데이터베이스 이름과 계정입니다. 서버를 실행한 뒤에는 바꾸기 어렵습니다. 비밀번호는 설치 때 무작위로 만들어집니다 |
| `POSTGRES_HOST`, `POSTGRES_PORT` | compose 네트워크의 서비스 이름(`db`)에 맞춘 값이므로 바꾸지 않습니다 |
| `CELERY_BROKER_URL` | 작업 대기열이 쌓이는 Redis 주소입니다 |
| `MEDIA_ROOT`, `MEDIA_URL` | 업로드 원본을 저장하는 경로와 URL입니다 |
| `EMBEDDING_INTERNAL_TOKEN` | app과 embedding-executor 사이의 내부 호출에만 쓰는 토큰입니다. 설치 때 무작위로 만들어지며 브라우저에는 전달되지 않습니다 |
| `EXECUTOR_INTERNAL_TOKEN` | executor 전용 토큰을 따로 두고 싶을 때만 지정합니다. 비워 두면 `EMBEDDING_INTERNAL_TOKEN`을 씁니다 |
| `EMBEDDING_SERVICE_PORT` | embedding-executor 내부 HTTP 포트입니다. 호스트에는 노출되지 않습니다 |
| `RAG_LLM_URL` | 로컬 LLM 런타임의 주소입니다. 기본값은 `http://rag-runtime:8080`입니다 |
| `DOTORI_OPERATION_MODE` | 설치 모드를 나타냅니다. 설치 절차가 정하므로 직접 바꾸지 않습니다 |
| `SERVICE_NAME` | 서비스별로 로그 파일 이름을 나누는 값입니다. docker-compose.yml이 서비스마다 지정하며 기본값 `app`은 로컬에서 직접 실행할 때만 쓰입니다 |
| `LOG_DIR` | 컨테이너 안의 로그 저장 경로입니다. `./data/logs`로 마운트됩니다 |

## 운영자가 바꾸는 값

### 접속과 인증

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `LOGIN_REQUIRED` | `0` | 0이면 로그인 없이 로컬 관리자 프로필로 자동 접속합니다. 외부 접속이 없는 개인 사용의 기본값입니다. 1이면 실제 로그인을 요구하며 외부에서 접속하는 배포에 씁니다. `python install.py --login enable\|disable`로 바꾸면 재시작 여부를 묻습니다 |
| `DOTORI_DJANGO_CSRF_TRUSTED_ORIGINS` | 로컬 주소 목록 | 변경 요청의 Origin 검사를 통과시킬 주소 목록입니다. 외부 배포에서는 실제 HTTPS 주소로 바꿉니다 |
| `DOTORI_SPA_ENABLED` | `1` | 0으로 하면 root URL을 이전 Django 템플릿 화면(`/files/`)으로 되돌립니다 |
| `DJANGO_DEBUG` | `0` | Django 디버그 모드입니다 |
| `DJANGO_TIME_ZONE`, `DJANGO_LANGUAGE_CODE` | `Asia/Seoul`, `ko-kr` | 서버의 시간대와 기본 언어입니다 |

### 메일

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `REQUIRE_EMAIL_VERIFICATION` | `1` | 가입할 때 이메일 인증을 요구합니다 |
| `DJANGO_EMAIL_BACKEND` | console 백엔드 | 메일을 보내는 방식입니다. 기본값은 메일을 보내지 않고 서버 로그에 출력합니다 |
| `DEFAULT_FROM_EMAIL` | `noreply@example.com` | 발신 주소입니다 |

`.env`로는 SMTP 서버 주소와 계정을 지정할 수 없습니다. 실제로 메일을 보내려면 서버 설정 코드를 직접 고쳐야 합니다.

### 모델 내려받기

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `HF_TOKEN` | 없음 | Hugging Face 토큰입니다. 필수는 아니고 일부 모델을 내려받을 때 필요합니다 |
| `HF_HUB_OFFLINE`, `TRANSFORMERS_OFFLINE` | `0` | 1로 하면 원격 요청을 막고 로컬 캐시만 씁니다. 인터넷이 없는 환경에서 씁니다 |

### 문서 처리

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `DOCUMENT_PARSER_BACKEND` | `docling` | 문서 파싱에 쓸 구현입니다. 등록된 것만 지정할 수 있고 현재는 `docling` 하나입니다 |
| `EMBEDDING_TOKEN_HEADROOM` | `128` | 청크를 나눌 때 임베딩 입력 상한에서 안전하게 비워 두는 여유입니다 |
| `DOCUMENT_AI_EMBED_QUEUE_BACKPRESSURE_LIMIT` | `32` | embed 큐에 이만큼 작업이 쌓이면 parse 작업은 파일을 열기 전에 재시도합니다. Redis 점검이 실패하면 업로드가 막히지 않도록 그대로 파싱합니다 |
| `DOCUMENT_AI_PARSE_BACKPRESSURE_RETRY_SECONDS` | `5` | 위 상황에서 parse 작업을 다시 시도하기까지의 대기 시간(초)입니다 |

`PARSER_TOKENIZER_ID`, `PARSER_TOKENIZER_REVISION`, `EMBEDDING_MAX_TOKENS`는 런타임 파일이 없을 때만 쓰는 비상값이며 `.env.example`에서 주석 처리되어 있습니다. 보통 바꾸지 않습니다.

### 파이프라인 복구

`repair_document_pipeline` 관리 명령이 쓰는 값입니다. 사용법은 [운영 가이드](../guides/operate/operation-guide.md)에 있습니다.

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `DOCUMENT_AI_RECOVERY_STALE_MINUTES` | `30` | 중지된 작업을 복구 대상으로 보기까지 기다리는 시간(분)입니다 |
| `DOCUMENT_AI_RECOVERY_PARSE_BATCH_SIZE` | `50` | 명령 한 번에 파싱을 다시 대기열에 넣는 최대 건수입니다 |
| `DOCUMENT_AI_RECOVERY_EMBED_BATCH_SIZE` | `200` | 명령 한 번에 임베딩을 다시 대기열에 넣는 최대 청크 수입니다 |
| `DOCUMENT_AI_MAX_RECOVERY_ATTEMPTS` | `5` | 복구를 시도하는 최대 횟수입니다. 넘으면 복구 대상에서 영구히 제외됩니다 |

### 임베딩 서비스와 자원

임베딩이 느리거나 거부될 때 조정합니다. 서버 하드웨어의 여유 안에서만 늘립니다.

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `EMBEDDING_SERVICE_TIMEOUT` | `60` | app이 embedding-executor에 보내는 임베딩 요청의 제한 시간(초)입니다 |
| `EMBEDDING_BATCH_SERVICE_TIMEOUT` | `180` | 문서 배치 요청의 제한 시간(초)입니다. CPU 추론을 고려해 따로 둡니다. 재시작 뒤 첫 호출은 모델을 읽느라 수십 초가 걸릴 수 있습니다 |
| `EMBEDDING_MODEL_CONCURRENCY` | `2` | 모델이 동시에 처리하는 `encode()` 호출 수입니다 |
| `EMBEDDING_REQUEST_QUEUE_CAPACITY` | `4` | 모델 앞에서 기다릴 수 있는 요청 수입니다. 질의가 문서보다 먼저, 같은 종류는 먼저 온 순서로 처리됩니다 |
| `EMBEDDING_QUERY_QUEUE_RESERVE` | `1` | 문서 요청이 대기열을 모두 채우지 못하도록 질의 전용으로 남기는 자리입니다 |
| `EMBEDDING_REQUEST_QUEUE_TIMEOUT_SECONDS` | `15` | 대기열에서 기다리는 제한 시간(초)입니다. 넘으면 서비스가 바쁘다는 응답을 돌려줍니다 |
| `EMBEDDING_DOCUMENT_BATCH_MAX_CHUNKS` | `4` | 같은 문서의 청크를 한 번의 `encode()`로 묶는 최대 개수입니다. 질의는 묶지 않습니다 |
| `EMBEDDING_DOCUMENT_BATCH_MAX_TOKENS` | `3200` | 한 번에 묶는 토큰 수의 상한입니다. 개수와 토큰 중 먼저 닿는 쪽에서 끊습니다. 배치가 클수록 그 사이 도착한 질의가 더 오래 기다립니다 |
| `EMBEDDING_HTTP_THREADS` | `8` | 실행, 대기, 헬스체크 요청을 함께 받는 스레드 수입니다. 모델은 worker 하나에만 올려야 하므로 worker 수는 늘리지 않습니다 |
| `PARSE_WORKER_CPU_SHARES` | `256` | parser-executor의 상대적 CPU 우선순위입니다. Docker의 기본값은 1024이고 app과 LLM 컨테이너는 기본값을 씁니다. 값이 작을수록 CPU가 몰릴 때 문서 처리가 양보합니다. 하드 상한은 아닙니다 |
| `EMBEDDING_MODEL_CPU_SHARES` | `256` | embedding-executor의 상대적 CPU 우선순위입니다. 의미는 위와 같습니다 |

### 웹과 RAG 서버

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `WEB_CONCURRENCY` | `2` | 웹 프로세스 수입니다. RAG 동시성과는 별개입니다 |
| `GUNICORN_TIMEOUT` | `360` | 웹 worker의 장애를 감지하는 제한 시간(초)입니다 |
| `RAG_REQUEST_TIMEOUT` | `300` | 답변 생성의 제한 시간(초)입니다 |
| `RAG_EXTERNAL_LLM_CONCURRENCY` | `4` | 외부 LLM 엔드포인트별 동시 요청 상한입니다 |

### 로그

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `OPERATION_LOG_LEVEL` | `WARNING` | 운영 문제를 확인하는 `operations.{SERVICE_NAME}.log`의 수준입니다 |
| `DOCUMENT_AI_LOG_LEVEL` | `INFO` | 파이프라인 흐름을 확인하는 `documents.{SERVICE_NAME}.log`의 수준입니다 |

## 검색과 RAG 튜닝

검색 결과의 품질과 답변의 모양을 바꾸는 값입니다. 기본값으로 충분하며, 바꾸기 전에 [모니터링 가이드](../guides/operate/monitoring-and-quality-guide.md)의 품질 점검으로 효과를 확인합니다.

### 하이브리드 검색

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `EMBEDDING_HYBRID_DENSE_WEIGHT`, `EMBEDDING_HYBRID_SPARSE_WEIGHT` | `0.7`, `0.3` | dense와 sparse 점수의 가중치입니다. 합이 1.0이 되도록 권장합니다 |
| `EMBEDDING_HYBRID_CANDIDATE_MULTIPLIER` | `12` | dense 1차 후보 수는 `top_k`에 이 값을 곱한 만큼입니다 |
| `EMBEDDING_PER_NODE_CANDIDATE_CAP` | `4` | 문서 하나당 dense 후보로 허용하는 최대 개수입니다 |
| `EMBEDDING_QUERY_SPARSE_TOP_N` | `32` | 질의의 sparse 벡터에서 상위 N개 토큰만 씁니다 |
| `EMBEDDING_DOC_POOLING_METHOD` | `normalized_logsumexp` | 청크 점수를 문서 점수로 합치는 방식입니다. `normalized_logsumexp`, `normalized_softmax`, `max` 중에서 고릅니다 |
| `EMBEDDING_DOC_POOL_TAU` | `10.0` | `normalized_logsumexp`의 온도입니다. 1~20이며 높을수록 `max`에 가까워집니다 |
| `EMBEDDING_DOC_POOL_TOP_K` | `5` | 문서 점수를 합칠 때 쓰는 청크 수입니다 |
| `EMBEDDING_DOC_LENGTH_PENALTY_ALPHA` | `0.0` | 문서 길이에 대한 감점 강도입니다. 0이면 감점하지 않습니다 |
| `EMBEDDING_EVIDENCE_TOP_K` | `3` | 결과에 포함할 근거 청크 수입니다 |
| `EMBEDDING_EVIDENCE_CONTEXT_WINDOW` | `1` | 근거에 함께 넣을 인접 청크 수입니다. 0이면 해당 청크만 씁니다 |

### 문맥 압축

`CONTEXTUAL_COMPRESSION_ENABLED`(기본 `0`)를 1로 하면, 검색된 청크를 구간으로 나눠 질문과 점수가 높은 구간만 남깁니다. 아래 값은 켰을 때만 의미가 있습니다.

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `CONTEXTUAL_COMPRESSION_WINDOW_SIZE` | `2` | 한 구간으로 묶는 문장 수입니다. 청크를 문장 단위로 나눠 이만큼씩 묶어 평가합니다 |
| `CONTEXTUAL_COMPRESSION_MAX_SEGMENTS_PER_CHUNK` | `16` | 청크 하나에서 만드는 구간의 최대 수입니다 |
| `CONTEXTUAL_COMPRESSION_TOP_SEGMENTS` | `3` | 남기는 구간의 수입니다 |
| `CONTEXTUAL_COMPRESSION_MAX_CHARS` | `700` | 남기는 글자 수의 상한입니다 |
| `CONTEXTUAL_COMPRESSION_MIN_SCORE` | `0.1` | 이 점수보다 낮은 구간은 버립니다 |
| `CONTEXTUAL_COMPRESSION_DENSE_WEIGHT`, `CONTEXTUAL_COMPRESSION_SPARSE_WEIGHT` | `0.4`, `0.6` | 구간 점수에서 dense와 sparse의 비중입니다 |

### RAG 답변

| 항목 | 기본값 | 설명 |
| --- | --- | --- |
| `RAG_SEARCH_TOP_K` | `3` | 근거로 검색하는 결과의 상위 개수입니다 |
| `RAG_RETRIEVAL_THRESHOLD` | `0.25` | 근거로 인정하는 검색 점수의 하한입니다 |
| `RAG_EVIDENCE_LIMIT` | `3` | 프롬프트에 넣는 근거 청크의 개수입니다 |
| `RAG_CONTEXT_MAX_CHARS` | `3000` | 근거로 쓰는 글자 수의 총 상한입니다 |
| `RAG_EVIDENCE_TEXT_MAX_CHARS` | `500` | 청크 하나당 프롬프트에 넣는 글자 수의 상한입니다 |
| `RAG_MAX_TOKENS` | `2048` | 답변 생성의 최대 토큰 수입니다 |
| `RAG_TEMPERATURE`, `RAG_TOP_P` | `0.2`, `0.9` | 답변 생성의 샘플링 설정입니다 |

## 실험 단계의 값

`QUERY_UNDERSTANDING_*`는 질의 이해 파이프라인의 실험 설정이며 사용을 권장하지 않습니다. 기본은 모두 꺼져 있습니다(`QUERY_UNDERSTANDING_PIPELINE_ENABLED=0`, `QUERY_UNDERSTANDING_LLM_ENABLED=0`).

## install.py 명령

`python install.py`를 옵션 없이 실행하면 설치 마법사가 시작됩니다. 이미 설치된 서버에는 아래 옵션을 씁니다.

### 서비스 제어

| 옵션 | 설명 |
| --- | --- |
| `--run` | 저장된 설정으로 서비스를 시작합니다 |
| `--restart` | 서비스를 멈췄다가 다시 시작합니다. `.env`를 고친 뒤 반영할 때 씁니다 |
| `--rebuild` | 이미지를 다시 빌드하고 활성 런타임을 재시작합니다 |
| `--stop` | 서비스를 멈춥니다. 컨테이너는 보존됩니다 |
| `--shutdown` | 컨테이너와 네트워크를 완전히 제거합니다 |
| `--status` | 서버 상태를 보여 줍니다. `--json-output`으로 기계가 읽는 형식을 얻고 `--skip-file-io`로 파일 I/O 점검을 건너뜁니다 |
| `--accounts list` | 로컬 계정 목록을 보여 줍니다 |

### 모델과 설정 변경

| 옵션 | 설명 |
| --- | --- |
| `--change-llm` | LLM 런타임 선택 마법사를 실행합니다. `--cluster-mode`로 클러스터 모드 탐지를 쓸 수 있습니다 |
| `--retry-llm` | 로컬 LLM 런타임의 시작을 다시 시도합니다 |
| `--remove-llm` | 현재 LLM 런타임을 중지하고 제거합니다 |
| `--change-embedding` | 검증된 새 임베딩 런타임 세대를 만들어 활성화합니다 |
| `--login enable\|disable` | 실제 로그인을 요구하거나, 로그인 없는 개인 모드로 되돌립니다 |

`--change-embedding`과 함께 쓰는 옵션은 다음과 같습니다.

| 옵션 | 설명 |
| --- | --- |
| `--embedding-priority` | 서버 전체에 적용할 임베딩 카탈로그의 프리셋을 고릅니다 |
| `--catalog-id` | 카탈로그의 임베딩 모델 ID를 지정합니다. 예: `bge-m3-hybrid` |
| `--external-url`, `--external-model`, `--external-key` | 외부 OpenAI 호환 임베딩 엔드포인트의 주소, 모델 이름, API 키(선택)입니다 |
| `--skip-reembed` | 새 런타임을 활성화할 때 기존 문서의 재임베딩을 건너뜁니다 |
| `--force-reembed` | 이미 활성인 모델이어도 기존 문서를 다시 임베딩합니다 |

모델 변경의 전체 절차는 [임베딩 런타임 가이드](../guides/operate/embedding-guide.md)와 [LLM 설치 가이드](../guides/install/llm-installation-guide.md)가 다룹니다.

### 공통 옵션과 외부 접속

| 옵션 | 설명 |
| --- | --- |
| `--scope` | 대상 런타임 범위를 고릅니다. 기본값은 `production`입니다 |
| `--keep-weights` | 런타임을 바꿀 때 내려받은 모델 가중치를 보존합니다 |
| `--yes` | 확인 질문을 건너뜁니다(`--remove-llm`, `--login`) |
| `--network-access-create`, `--network-access-open` | 외부 접속 설정 파일을 만들거나 그 폴더를 엽니다 |
| `--network-access-connect`, `--network-access-disconnect` | 외부 접속 모듈을 연결하거나 끊습니다 |
| `--network-access-status` | 외부 접속 상태를 보여 줍니다 |
