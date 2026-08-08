# 도토리 문서

도토리는 문서 관리, 하이브리드 검색, 검색 증강 생성(RAG)을 한 서버에서 운영하는 셀프호스팅 문서 작업 공간입니다. 문서, 검색 인덱스, 로컬 AI 런타임을 운영자가 관리하는 Docker Compose 환경 안에 보관합니다.

<p align="center">
  <img src="https://github.com/user-attachments/assets/263cba6d-04f6-49ba-9ccb-85481157539a" width="80%" alt="도토리 문서 작업 공간">
</p>

<p align="center">
  <img src="https://github.com/user-attachments/assets/d240cf34-1dfb-462e-8366-3f0d3e4a435f" width="80%" alt="도토리 RAG 작업 공간">
</p>

영문 문서는 [README.md](README.md)를 참고하세요. 

상세 내용은 [Walkthrough](./documents/WALKTHROUGH.md)을 참고하세요.

## 주요 기능

- 여러 개의 계정을 생성하여 파일과 문서 기능을 안전하게 분리 가능
- 인증, 휴지통, 즐겨찾기, 최근 파일을 포함한 폴더 및 파일 관리
- PDF, HWP, DOCS 등 텍스트 파일의 분석 기능
- 자연어 검색을 통한 문서 검색 기능
- 외부로 정보가 유출되지 않고 문서 내용을 기반으로 답변을 제공하는 RAG 기능
- 컴퓨터 사양을 바탕으로 직접 고르는 로컬 LLM 설치 및 사용 기능
- 파일 관리 전용부터 전체 로컬 RAG까지 선택할 수 있는 세 가지 설치 모드
- 한국어와 영어 웹 UI와 외부 AI 모델 연동을 위한 기능

!주의
로컬 LLM은 사용자가 직접 선택해야 하며, AI 모델과 런타임은 사용자별 설정이 아니라 서버 전체 설정으로 관리됩니다.
외부 LLM(ChatGPT, Claude 등)을 선택하는 경우, 문서 내용이 외부로 제공될 수 있습니다. 


## 설치 모드

시스템에 제공하는 설치 도우미(start.bat)을 통해 코딩 없이 서버를 설치할 수 있습니다.
관련 내용은 [설치 가이드](docs/installation-user-guide.md)를 참고하세요.

설치 모드에서 제공하는 옵션은 다음과 같습니다:
1. 선택적 기능 활성화(자연어 검색 기능, RAG 기능을 끄고 킬 수 있습니다)
2. 로컬 LLM 설치 가이드
3. 서버 운영 환경설정 가이드
4. 서버 외부접속 도메인설정


## 빠른 시작

### 요구 사항

- Docker Engine 또는 Docker Desktop
- Python 3.x
- vLLM GPU 런타임 사용 시 NVIDIA 드라이버와 NVIDIA Container Toolkit
- 선택한 모델이 인증을 요구할 경우 Hugging Face 토큰

Windows에서는 Docker Desktop의 WSL2 백엔드를 권장합니다.


## 시스템 구조

```text
      Nginx
        |
     Web App -------------- DataBase
        |
    Queue Server
        |
        +-- embedding-worker  [parse, embed]
        +-- search-worker     [search]
        +-- rag-worker        [rag] ---- 선택된 로컬 런타임
                                          |-- llama-rag (llama.cpp)
                                          `-- vllm-rag  (vLLM)
```

각 worker로 구성된 Queue Server는 설정된 동시성을 바탕으로 작업을 비동기적으로 처리합니다.


## 데이터와 설정

- `.env.example`에는 운영에 필요한 주요 설정값이 담겨 있습니다. 이 파일을 복사하여 `.env` 파일을 만들고 필요에 따라 수정하세요. 
- 또한 `.env.example`에는 고급 기능들을 위한 제어 설정들이 포함되어 있습니다. 
- 해당 프로그램을 사용하며 저장한 모든 파일들은 로컬 서버의 파일시스템에 저장됩니다. 별다른 백업 기능은 아직 제공하지 않고 있습니다.
- `data/config/llm_runtime.json`은 로컬 RAG 설정 과정에서 생성되는 서버 단위 런타임 기준 파일입니다.

## 개발

저장소의 주요 애플리케이션 경계는 `files`, `accounts`, `document_ai`입니다. 
무거운 AI 작업은 processing, embedding, query-understanding, search, RAG, 설치 및 런타임 모듈로 분리되어 있습니다.

변경 사항을 확인하기 위한 테스트 코드는 일부 제공됩니다.
```bash
docker compose --profile test run --rm test python manage.py check
docker compose --profile test run --rm test python -m pytest
```

## 프로젝트 상태

도토리는 현재 개발 중입니다. 
현 소스에는 소개된 기능들이 제공되고 있으며, 실험적 기능들이 포함되어 있습니다.
버그 및 기능 건의는 이슈 혹은 이메일을 통해 부탁드립니다.

## 라이선스

[LICENSE](LICENSE)를 참고하세요.
