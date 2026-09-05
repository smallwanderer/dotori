# 도토리 문서 설치 가이드

해당 문서는 도토리 서버를 직접 설치하는 방법에 대해 설명합니다. 
로컬 LLM 모델 선택 및 교체 등 AI 런타임에 특화된 내용은 [LLM 설치 가이드](./llm-installation-guide.md)를,
설치 이후의 운영 가이드라인은(시작/중지/계정/백업/보안)은 [운영 가이드](./operation-guide.md)를 참고하세요.

## 목차

1. [사전 준비물](#1-사전-준비물)
2. [소스 코드 받기](#2-소스-코드-받기)
3. [설치 실행](#3-설치-실행)
4. [.env 설정](#4-env-설정)
5. [설치 후 접속 확인](#5-설치-후-접속-확인)
6. [외부 접속 설정 (선택)](#6-외부-접속-설정-선택)
7. [자주 발생하는 설치 문제](#7-자주-발생하는-설치-문제)
8. [다음 단계](#8-다음-단계)

---

## 1. 사전 준비물

| 항목 | 필요 여부 | 비고 |
|------|-----------|------|
| Docker Engine 또는 Docker Desktop | 필수 | Windows는 WSL2 백엔드 사용을 권장합니다. |
| Docker Compose (Compose V2, `docker compose`) | 필수 | 최신 Docker Desktop에는 기본 포함되어 있습니다. |
| Python 3.x | 필수 | `install.py` 설치 마법사를 호스트에서 직접 실행하는 데 사용합니다. |
| NVIDIA 드라이버 + NVIDIA Container Toolkit | 선택 | vLLM(GPU 전용) 런타임을 사용할 경우에만 필요합니다. |
| Hugging Face 토큰 | 선택 | 접근 제한(gated)이 걸린 모델을 다운로드할 때 필요합니다. |

디스크 여유 공간과 RAM/VRAM 요구량은 선택하는 모델에 따라 달라집니다.
설치 마법사 첫 단계에서 현재 하드웨어의 상태를 자동으로 진단해 보여주므로, 정확한 수치는 그 진단 결과를 기준으로 판단하세요.

---

## 2. 소스 코드 받기

```bash
git clone https://github.com/smallwanderer/dotori.git
cd dotori
```

---

## 3. 설치 실행

### Windows

`start.bat`을 실행한 뒤 `[1] Install / Setup Wizard`를 선택합니다.

### macOS / Linux (또는 Windows에서 직접 실행)

```bash
python install.py
```

### 마법사 진행 순서

`.env`가 없으면 마법사는  `.env.example`를 바탕으로 `.env` 파일을 자동으로 생성합니다.

1. **하드웨어 진단** — OS, CPU, RAM, 디스크 여유 공간, GPU, Docker 사용 가능 여부를 출력합니다.

2. **운영 모드 선택**
   | 옵션 | 설명 |
   |------|------|
   | `[1] Full 로컬 AI RAG 모드` | 로컬 LLM 답변 생성 + 임베딩 + 하이브리드 검색 전체 기능 |
   | `[2] Hybrid/Search AI 모드` (기본값) | LLM 답변 생성 없이 임베딩과 하이브리드 검색만 사용 |
   | `[3] 기본 모드` | AI 기능 없이 파일 업로드·관리만 사용 |

3. **임베딩 모델 선택** ([1], [2]에서만)
   - `BAAI/bge-m3` (기본, 고품질 하이브리드 검색)
   - `intfloat/multilingual-e5-small` (경량, CPU 권장)

4. **RAG 우선순위 선택** ([1]에서만)
   - `speed`: 빠른 응답, 속도 우선
   - `balanced` (기본값): 속도와 품질의 균형
   - `quality`: 높은 품질, 정확도 우선

5. 선택 내용이 `.env`에 기록되고, 서버 설치 전 모든 사용자 입력이 완료됩니다.

6. 지금 바로 서비스를 시작할지 확인합니다(`y/N`, 기본값은 N). 
   - `.env` 파일이 없었다면, 이 단계에서 비밀번호와 같은 랜덤 설정값을 포함한 `.env` 파일이 생성됩니다.
   - 몇몇 옵션은 서버 시작시 변경이 어려우므로 확인을 위해서는 'n'을 입력하고 재확인해주세요.
   - 자세한 내용은 [4. .env 설정](#4-env-설정)을 참고하세요.
   - 시작을 선택하면 Docker 이미지를 빌드하고 컨테이너를 기동합니다.

7. [1]번 모드를 선택했다면, 컨테이너가 뜬 뒤 서버 내부에서 LLM 런타임을 자동으로 감지·설정합니다(`detect_llm_runtime --interactive`).

   이 단계의 상세 동작과 수동 재설정 방법은 [LLM 설치 가이드](./llm-installation-guide.md)를 참고하세요.

설치를 마친 뒤 이미지를 새로 빌드하지 않고 저장된 설정 그대로 재시작하려면 다음을 사용합니다.

```bash
python install.py --run
```

---

## 4. .env 설정

설치 마법사가 `.env.example`을 복사해 `.env`를 처음 만들 때, 다음 항목은 자동으로 채워지므로 직접 입력할 필요가 없습니다.

| 항목 | 처리 방식 |
|------|-----------|
| `DJANGO_SECRET_KEY` | 랜덤 값으로 자동 생성됩니다. |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` | `POSTGRES_USER`는 `dotori`로 고정되고, `POSTGRES_PASSWORD`는 랜덤 값으로 자동 생성됩니다. |
| `HF_TOKEN` | 마법사가 입력을 물어봅니다. 접근 제한(gated) 모델을 쓰지 않는다면 Enter로 건너뛰면 됩니다. |
| 선택한 운영 모드에 따른 값 (임베딩 모델, `QUERY_LLM_ENABLED` 등) | 앞선 모드/임베딩 모델 선택에 맞춰 자동으로 채워집니다. |

> `.env.example`을 직접 복사해서 `python install.py` 없이 수동으로 `.env`를 만드는 경우에는 위 값들이 자동으로 채워지지 않으므로, `DJANGO_SECRET_KEY`와 `POSTGRES_PASSWORD`를 직접 고유한 값으로 바꿔야 합니다.

그 외 값 중 확인해 둘 만한 것은 다음과 같습니다.

| 항목 | 설명 |
|------|------|
| `LOGIN_REQUIRED` | 기본값은 `0`(로그인 없이 로컬 관리자로 자동 접속)입니다. 외부에서 접속 가능하게 만들 계획이라면 `1`로 전환해야 합니다 — [6장](#6-외부-접속-설정-선택)과 [운영 가이드 4장](./operation-guide.md#4-관리자-계정-및-사용자-관리)을 참고하세요. |
| 그 외 `EMBEDDING_*`, `RAG_*`, `QUERY_*` 값들 | 설치 마법사가 운영 모드에 맞춰 채워주는 기본값으로 시작 가능하며, 검색/RAG 품질을 튜닝할 때만 조정하면 됩니다. |
| `CONTEXTUAL_COMPRESSION_*` (8개) | 검색 결과를 압축해 RAG 컨텍스트를 줄이는 기능의 세부 파라미터입니다. `.env.example`에 기본값이 이미 채워져 있고 `CONTEXTUAL_COMPRESSION_ENABLED=0`(비활성)이 기본값입니다. 켜는 방법과 각 값의 의미는 [운영 가이드 6.3장](./operation-guide.md#63-contextual-compression-설정)을 참고하세요. |
| `DOCUMENT_AI_WORKER_CONCURRENCY`, `EMBEDDING_HTTP_THREADS` | 문서 파싱·임베딩 처리 동시성입니다. 기본값 그대로 시작해도 되며, 대기열이 계속 쌓일 때 조정하는 방법은 [운영 가이드 5.4장](./operation-guide.md#54-처리-큐-튜닝)을 참고하세요. |

`.env`를 직접 수정한 뒤에는 컨테이너 재시작이 있어야 반영됩니다(`python install.py --restart`).

---

## 5. 설치 후 접속 확인

1. 브라우저에서 `http://127.0.0.1:8000`으로 접속합니다.
2. 기본 설정(`LOGIN_REQUIRED=0`)에서는 별도의 회원가입/로그인 없이 로컬 관리자 프로필로 자동 접속됩니다. 계정 전환이나 로그인 필수 전환 방법은 [운영 가이드 4장](./operation-guide.md#4-관리자-계정-및-사용자-관리)에서 다룹니다.
3. 서비스 상태(컨테이너, LLM 런타임, 검색/임베딩 기능 활성화 여부)를 확인하려면:

   ```bash
   python install.py --status
   ```

---

## 6. 외부 접속 설정 (선택)

기본 설치는 로컬(`127.0.0.1`)에서만 접근 가능합니다. 외부에서 접속 가능하게 만들려면 다음 명령으로 설정 파일을 준비하고 연결합니다.

```bash
python install.py --network-access-create   # 설정 파일 생성
python install.py --network-access-open     # 설정 폴더 열기 (도메인 등 값 입력)
python install.py --network-access-connect  # 외부 접속 모듈 연결
python install.py --network-access-status   # 현재 상태 확인
```

> [!CAUTION]
> **외부 접속을 연결하기 전**에 `python install.py --login enable`로 `LOGIN_REQUIRED=1`을 설정하세요. 
> - 기본값(`0`)은 외부 접속이 없는 개인/로컬 사용을 전제로, 접속 시 로그인 없이 로컬 관리자 권한이 자동으로 부여되는 모드입니다. 

### 6.1 설정 파일에 입력해야 하는 값

`--network-access-create`는 `data/config/network_access/provider.env`를 빈 값으로 생성하고, `--network-access-open`은 그 폴더를 열어줄 뿐입니다. 실제 값은 아래 표를 참고해 직접 입력해야 하며, 비어 있으면 `--network-access-connect`가 오류로 거부합니다.

| 키 | 설명 |
|---|---|
| `DOTORI_EXTERNAL_DOMAIN` | 외부에서 접속할 실제 도메인(예: `dotori.example.com`). DNS가 이미 이 서버의 IP를 가리키고 있어야 합니다. |
| `DOTORI_CERTIFICATE_EMAIL` | Let's Encrypt 인증서 발급·만료 알림을 받을 이메일 주소. |
| `DOTORI_DJANGO_ALLOWED_HOSTS` | Django가 허용할 호스트명. 위 도메인을 포함해야 하며, 빠지면 `DisallowedHost` 오류로 모든 요청이 거부됩니다. |
| `DOTORI_DJANGO_CSRF_TRUSTED_ORIGINS` | CSRF 검증을 통과시킬 origin. `https://` + 위 도메인 형식으로 적습니다. |
| `DOTORI_DJANGO_SECURE_PROXY_SSL_HEADER`, `_SECURE_SSL_REDIRECT`, `_SESSION_COOKIE_SECURE`, `_CSRF_COOKIE_SECURE` | 모두 HTTPS 사용을 전제로 기본값 `1`(켜짐)입니다. 이 값을 직접 낮추지 마세요 — HTTPS 없이 외부에 노출하는 구성은 지원하지 않습니다. |

### 6.2 Let's Encrypt 인증서 발급 (최초 1회 필수)

위 파일을 채우는 것만으로는 HTTPS가 동작하지 않습니다. `--network-access-connect`는 인증서가 **이미 존재한다고 가정**하고 Nginx를 실행하므로, 인증서가 없으면 Nginx가 아예 시작되지 않습니다. 인증서는 별도 스크립트로 발급합니다(Linux/macOS 또는 Windows의 Git Bash·WSL에서 실행).

```bash
bash scripts/init-letsencrypt.sh
```

이 스크립트가 순서대로 처리합니다: 임시 자체 서명 인증서로 Nginx를 먼저 띄움 → ACME 챌린지 경로가 응답하는지 확인 → 실제 Let's Encrypt 인증서 발급 → Nginx에 반영. 완료 후 `python install.py --network-access-status`로 상태를 확인하세요.

Let's Encrypt 인증서는 90일마다 만료되며, **자동 갱신은 설정되어 있지 않습니다.** 아래 스크립트를 주기적으로(예: 매월 1회) 직접 실행하거나 cron 등으로 예약해야 합니다.

```bash
bash scripts/renew-letsencrypt.sh
```

### 6.3 연결 및 해제

외부 접속을 해제하려면 `python install.py --network-access-disconnect`를 실행합니다. 
자세한 내용은 `start.bat` → `[7] Advanced Network Settings` 메뉴에서도 동일하게 접근할 수 있습니다(단, `init-letsencrypt.sh`/`renew-letsencrypt.sh`는 셸 스크립트라 `start.bat` 메뉴에는 포함되어 있지 않으며 Git Bash나 WSL에서 직접 실행해야 합니다).

---

## 7. 자주 발생하는 설치 문제

| 증상 | 확인 사항 |
|------|-----------|
| 이미지 빌드/컨테이너 시작 실패 | Docker Desktop이 실행 중인지 확인합니다. 빌드 실패 시 콘솔에 출력되는 전체 로그를 확인하세요. |
| `8000` 포트 충돌 | 다른 프로세스가 이미 8000번 포트를 사용 중인지 확인하고, Docker Desktop 설정에서 포트를 변경하거나 재시작해 보세요. |
| 로컬 LLM 헬스체크 실패 (모드 1) | 핵심 서비스(웹/검색)는 정상 동작하며 RAG 답변 생성만 비활성화됩니다. 원인 확인과 재시도 절차는 [LLM 설치 가이드 9장](./llm-installation-guide.md#9-트러블슈팅)을 참고하세요. |
| `LLM runtime is not configured` 오류 | 로컬 LLM 설정이 아직 완료되지 않은 상태입니다. [LLM 설치 가이드 4장](./llm-installation-guide.md#4-llm-런타임-설정)의 수동 설정 절차를 따르세요. |

---

## 8. 다음 단계

- 로컬 LLM 모델 선택·변경·상세 설정: [LLM 설치 가이드](./llm-installation-guide.md)
- 서비스 시작/중지, 계정 관리, 백업, 보안 체크리스트: [운영 가이드](./operation-guide.md)
