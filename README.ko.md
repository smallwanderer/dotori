# 도토리 문서

도토리는 문서 관리, 하이브리드 검색, 검색 증강 생성(RAG)을 한 서버에서 운영하는 셀프호스팅 문서 작업 공간입니다. 문서, 검색 인덱스, 로컬 AI 런타임을 운영자가 관리하는 Docker Compose 환경 안에 보관합니다.

<p align="center">
  <img src="https://github.com/user-attachments/assets/263cba6d-04f6-49ba-9ccb-85481157539a" width="80%" alt="도토리 문서 작업 공간">
</p>

<p align="center">
  <img src="https://github.com/user-attachments/assets/d240cf34-1dfb-462e-8366-3f0d3e4a435f" width="80%" alt="도토리 RAG 작업 공간">
</p>

영문 문서는 [README.md](README.md)를 참고하세요. 상세 설치 및 운영 절차는 [WALKTHROUGH.md](WALKTHROUGH.md)에 있습니다.

## 주요 기능

- 인증, 휴지통, 즐겨찾기, 최근 파일을 포함한 비공개 파일·폴더 관리
- PDF, Office, HWP/HWPX, 텍스트, Markdown, HTML, 일반 이미지 파일의 비동기 파싱·청킹·임베딩
- PostgreSQL과 pgvector를 이용한 dense/sparse 하이브리드 검색
- 근거 문서, 스트리밍 출력, 작업 취소, 진행 상태를 제공하는 RAG
- CPU, RAM, GPU, VRAM, 디스크 용량을 탐지하는 서버 단위 로컬 LLM 선택
- `speed`, `balanced`, `quality` 프리셋에 따른 `llama.cpp` 또는 vLLM 실행 계획 자동 생성
- 파일 관리 전용부터 전체 로컬 RAG까지 선택할 수 있는 세 가지 설치 모드
- 한국어·영어 웹 UI와 외부 클라이언트용 동기화 API

## 설치 모드

설치 프로그램에서 운영 모드를 선택합니다. AI 모델과 런타임은 사용자별 설정이 아니라 서버 전체 설정으로 관리됩니다.

| 모드 | 실행 범위 |
| --- | --- |
| Full 로컬 AI RAG | 파일 관리, 파싱, 임베딩, 하이브리드 검색, 쿼리 처리, 로컬 RAG 답변 생성 |
| Hybrid/Search AI | 답변 생성 LLM을 제외한 파일 관리, 파싱, 임베딩, 하이브리드 검색 |
| 기본 모드 | 파일 관리만 사용 |

Full 모드에서 운영자가 선택하는 항목은 `speed`, `balanced`, `quality` 중 하나입니다. 도토리가 탐지한 하드웨어를 기준으로 모델, 백엔드, 양자화, 컨텍스트 길이, 동시성, 메모리 정책을 내부에서 결정합니다. GGUF 및 RAM 오프로딩 구성은 `llama.cpp`를 사용하고, 호환되는 NVIDIA GPU 또는 클러스터 구성은 vLLM을 사용할 수 있습니다.

## 빠른 시작

### 요구 사항

- Docker Engine 또는 Docker Desktop
- Docker Compose v2
- Python 3
- Git
- vLLM GPU 런타임 사용 시 NVIDIA 드라이버와 NVIDIA Container Toolkit
- 선택한 모델이 인증을 요구할 경우 Hugging Face 토큰

Windows에서는 Docker Desktop의 WSL2 백엔드를 권장합니다.

### 설치 프로그램 실행

```bash
git clone https://github.com/smallwanderer/dotori.git
cd dotori
python install.py
```

설치 프로그램은 `.env`를 만들고, 서버 하드웨어를 탐지한 뒤, `docker-compose.yml` 기준으로 선택한 모드에 필요한 서비스를 빌드하고 RAG 런타임을 설정합니다. 접속 주소는 다음과 같습니다.

```text
http://localhost/
```

컨테이너 실행 후 관리자 계정을 생성합니다.

```bash
docker compose -f docker-compose.yml exec app python manage.py createsuperuser
```

다음 실행부터는 `python install.py --run`을 사용하며 Windows에서는 `start.bat`도 사용할 수 있습니다. 로컬 RAG 모델이나 런타임을 명시적으로 변경하려면 다음 명령을 실행합니다.

```bash
python install.py --change-llm
```

도메인, HTTPS, 환경별 설치 절차는 [WALKTHROUGH.md](WALKTHROUGH.md)를 따르세요.

## 시스템 구조

```text
브라우저 / 동기화 클라이언트
        |
      Nginx
        |
   Django app -------------- PostgreSQL + pgvector
        |
      Redis
        |
        +-- embedding-worker  [parse, embed]
        +-- search-worker     [search]
        +-- query-worker      [query]
        +-- rag-worker        [rag] ---- 선택된 로컬 런타임
                                          |-- llama-rag (llama.cpp)
                                          `-- vllm-rag  (vLLM)
```

Django는 웹 애플리케이션, API, 작업 등록을 담당합니다. 파싱, 임베딩, 검색, 쿼리 처리, 답변 생성은 전용 Celery 큐에서 실행합니다. RAG 런타임은 선택된 하나만 활성 상태로 유지해야 합니다. 확정된 설정은 `data/config/llm_runtime.json`에 저장되며 일반 RAG 요청은 하드웨어를 다시 탐지하거나 모델을 다시 선택하지 않고 이 설정을 사용합니다.

## 자주 사용하는 명령

```bash
# 설치 스택 시작 또는 다시 빌드
docker compose -f docker-compose.yml up --build

# 데이터베이스 마이그레이션
docker compose -f docker-compose.yml exec app python manage.py migrate

# 특정 서비스 로그 확인
docker compose -f docker-compose.yml logs -f rag-worker

# 저장된 LLM 런타임 설정 확인
docker compose -f docker-compose.yml exec app \
  python manage.py inspect_llm_runtime
```

## 데이터와 설정

- `.env`는 설치 프로그램이 관리하는 스택의 설정 파일이며, `.env.dev`는 개발 전용 실행에만 사용합니다.
- `data/uploads/`, `data/pgdata/`, `data/logs/`, `data/config/`에는 로컬 영속 데이터가 저장되며 Git에 커밋하면 안 됩니다.
- `data/config/llm_runtime.json`은 로컬 RAG 설정 과정에서 생성되는 서버 단위 런타임 기준 파일입니다.
- 기본 임베딩 구성은 1024차원 dense 벡터와 sparse lexical weight를 함께 사용하는 BGE-M3입니다.
- 고급 검색 및 worker 설정은 `.env.example`에 있습니다. 특정 부하를 평가하는 경우가 아니라면 기본값으로 시작하세요.

## 개발

저장소의 주요 애플리케이션 경계는 `files`, `accounts`, `document_ai`입니다. 무거운 AI 작업은 processing, embedding, query-understanding, search, RAG, 설치·런타임 모듈로 분리되어 있습니다. 운영 지향 애플리케이션 이미지에는 테스트 의존성이 없으므로 기본 Compose 파일의 `test` 프로필을 사용합니다.

변경 사항을 제출하기 전에 관련 모듈의 집중 테스트를 실행하고, 가능하면 전체 테스트도 실행합니다.

```bash
docker compose --profile test run --rm test python manage.py check
docker compose --profile test run --rm test python -m pytest
```

## 프로젝트 상태

도토리는 현재 개발 중입니다. 현 소스에는 서버 단위 LLM 설치 서비스, 하이브리드 검색, 로컬·외부 RAG 엔드포인트 지원, 문서 작업 공간이 포함되어 있습니다. 운영 배포 전에는 TLS, 백업, 모니터링, 저장 공간, 모델 라이선스, 하드웨어 요구 사항을 환경에 맞게 검토해야 합니다.

## 라이선스

[LICENSE](LICENSE)를 참고하세요.
