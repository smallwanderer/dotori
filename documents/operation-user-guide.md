# Dotori for Document 운영 가이드

Dotori for Document(이하 Dotori)는 자가 호스팅 서비스로 개인 PC, NAS 서버, 클라우드 서버 등에 설치하여 RAG 기능을 이용하고, 고급 IR 기능을 이용할 수 있습니다.
다음 문서는 Dotori의 기본 운영 지침을 소개해드리고, 필요 시 확장할 수 있는 방법을 안내합니다.

---

## 목차

1. [개요](#1-개요)
2. [서비스 기동 및 중지](#2-서비스-기동-및-중지)
3. [상태 확인 및 모니터링](#3-상태-확인-및-모니터링)
4. [관리자 계정 및 사용자 관리](#4-관리자-계정-및-사용자-관리)
5. [문서 파이프라인 운영](#5-문서-파이프라인-운영)
6. [검색 및 RAG 설정](#6-검색-및-rag-설정)
7. [LLM 런타임 관리](#7-llm-런타임-관리)
8. [데이터 백업 및 복원](#8-데이터-백업-및-복원)
9. [업데이트](#9-업데이트)
10. [보안 체크리스트](#10-보안-체크리스트)
11. [트러블슈팅](#11-트러블슈팅)

---

## 1. 개요

Dotori는 단일 Docker Compose 스택으로 구동되는 자가 호스팅 문서 검색·RAG 서비스입니다.
운영자는 서버를 구동하고, 사용자 계정을 관리하며, 문서 처리 파이프라인과 LLM 런타임이 정상 동작하는지 확인할 수 있습니다.
AI 모델과 런타임 파라미터는 서버 단위로 한 번 설정되며, 사용자별로 변경할 수 없습니다.

### 1.1 기본 시스템 구성 요소

Dotori 스택은 다음 컴포넌트로 구성됩니다.

| 컴포넌트 | 역할 |
|----------|------|
| **app** (Django + Gunicorn) | 웹 UI, REST API, 파일 관리, 작업 등록을 담당합니다. |
| **db** (PostgreSQL + pgvector) | 파일 메타데이터, 청크 임베딩(dense/sparse), 작업 결과를 저장합니다. |
| **redis** | Celery 브로커. 워커 간 작업 큐와 동시성 제어(세마포어)를 담당합니다. |
| **embedding-worker** | `parse` · `embed` 큐를 처리합니다. 문서 파싱, 청킹, 임베딩을 수행합니다. |
| **search-worker** | `search` 큐를 처리합니다. 하이브리드 검색 요청을 실행합니다. |
| **query-worker** | `query` 큐를 처리합니다. 사용자 질문을 파싱하고 QueryDSL로 변환합니다. |
| **rag-worker** | `rag` 큐를 처리합니다. 검색 결과를 조합하고 LLM 런타임에 답변 생성을 요청합니다. |
| **llama-rag** | llama.cpp 기반 로컬 LLM 서버. GGUF 모델을 CPU 또는 GPU 혼합으로 실행합니다. |
| **vllm-rag** | vLLM 기반 로컬 LLM 서버. AWQ/GPTQ 모델을 NVIDIA GPU에서 실행합니다. |

> [!NOTE]
> `llama-rag`와 `vllm-rag` 중 **하나만 활성 상태**로 운영합니다.
> 어떤 런타임을 사용할지는 `data/config/llm_runtime.json`에 기록되며, 설치 시 자동으로 결정됩니다.

### 1.2 서비스 아키텍처

```text
   Django app (app) ----------- PostgreSQL + pgvector (db)
        |
      Redis
        |
        +-- embedding-worker   [parse, embed]
        +-- search-worker      [search]
        +-- query-worker       [query]
        +-- rag-worker         [rag] ---- 선택된 로컬 런타임
        |                                  |-- llama-rag  (llama.cpp)
        +-- recovery-worker               `-- vllm-rag   (vLLM)
            (Celery Beat)
```

무거운 AI 작업(파싱, 임베딩, 검색, 쿼리 처리, RAG 답변 생성)은 모두 전용 Celery 워커에서 실행됩니다.
Django `app`은 요청 수신과 작업 등록에만 집중하므로, 워커 수 조정이나 재시작이 웹 서비스 가용성에 직접 영향을 주지 않습니다.

### 1.3 운영 모드별 활성 서비스

설치 시 선택한 운영 모드에 따라 활성 서비스가 달라집니다.

| 모드 | 활성 서비스 | 비고 |
|------|------------|------|
| **Full 로컬 AI RAG** | Nginx, app, db, redis, embedding-worker, search-worker, query-worker, rag-worker, recovery-worker, llama-rag 또는 vllm-rag | LLM 답변 생성 포함 전체 기능 |
| **Hybrid/Search AI** | Nginx, app, db, redis, embedding-worker, search-worker, recovery-worker | LLM 없이 파싱·임베딩·하이브리드 검색만 동작 |
| **기본 모드** | Nginx, app, db, redis | 파일 관리만 사용 |

현재 설치된 모드는 `.env`의 설정값과 실행 중인 컨테이너 목록으로 확인할 수 있습니다.

```bash
docker compose ps
```

### 1.4 확장 서비스 요소

| 컴포넌트 | 역할 |
|----------|------|
| **Nginx** | 리버스 프록시. 외부 HTTP/HTTPS 요청을 `app`으로 전달하고 정적 파일·업로드를 직접 서빙합니다. |
| **recovery-worker** (Celery Beat) | 주기적으로 stale 상태의 파싱·임베딩 작업을 재큐잉합니다. |



---

## 2. 서비스 기동 및 중지

### 2.1 서비스 시작

### 2.2 서비스 중지

### 2.3 개별 서비스 재시작

### 2.4 환경변수 변경 후 반영

---

## 3. 상태 확인 및 모니터링

### 3.1 컨테이너 상태 확인

### 3.2 서비스별 로그 확인

### 3.3 LLM 런타임 상태 확인

### 3.4 문서 처리 파이프라인 상태 파악

---

## 4. 관리자 계정 및 사용자 관리

### 4.1 최초 관리자 계정 생성

### 4.2 Django Admin 접속

### 4.3 사용자 관리

---

## 5. 문서 파이프라인 운영

### 5.1 지원 파일 형식

### 5.2 업로드 → 파싱 → 임베딩 → 검색 흐름

### 5.3 처리 실패 항목 복구

### 5.4 처리 큐 튜닝

---

## 6. 검색 및 RAG 설정

### 6.1 하이브리드 검색 튜닝

### 6.2 RAG 파라미터 조정

### 6.3 Contextual Compression 설정

---

## 7. LLM 런타임 관리

### 7.1 현재 설정 확인

### 7.2 모델 변경

### 7.3 상세 설치·변경 절차

---

## 8. 데이터 백업 및 복원

### 8.1 백업 대상 경로

### 8.2 PostgreSQL 백업 및 복원

### 8.3 모델 캐시 경로

---

## 9. 업데이트

### 9.1 소스 업데이트 및 재빌드

### 9.2 마이그레이션 실행

### 9.3 .env 호환성 확인

---

## 10. 보안 체크리스트

### 10.1 Django Secret Key 변경

### 10.2 데이터베이스 비밀번호 변경

### 10.3 HTTPS / Let's Encrypt 설정

### 10.4 허용 호스트 및 CSRF 설정

### 10.5 HF_TOKEN 관리

---

## 11. 트러블슈팅

### 11.1 웹이 502를 반환함

### 11.2 문서 업로드 후 검색이 안 됨

### 11.3 RAG 답변이 생성되지 않음

### 11.4 recovery-worker가 계속 재큐잉함

### 11.5 디스크 또는 메모리 부족
