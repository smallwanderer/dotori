# Dotori 문서

Dotori는 운영자가 가진 파일을 미리 처리해 두고, 준비된 결과를 검색과 질의, 외부 AI 클라이언트가 다시 쓰게 하는 셀프호스팅 제품입니다.

## 어디서 시작하나요

| 추천 사용자 | 알고 싶은 것 | 시작점 |
| --- | --- | --- |
| Dotori 설치자 | 설치와 운영, 어떤 구성으로 동작하는지 | [Dotori란 무엇인가](overview/what-is-dotori.md) → [동작 방식](overview/how-it-works.md) → [설치 가이드](guides/install/installation-guide.md) |
| 통합 개발자 | API, CLI, 동기화 클라이언트로 연동하는 방법 | [동작 방식](overview/how-it-works.md) → [연동 가이드](guides/integrate/README.md) → [API 계약](reference/api-contract-v1.md) |
| 기여자 | 구조를 이해하고 바꾸는 방법 | [동작 방식](overview/how-it-works.md) → [아키텍처](architecture/README.md) |

## 목차 구성

| 목차 | 내용 |
| --- | --- |
| [overview/](overview/what-is-dotori.md) | Dotori가 무엇이고 어떻게 동작하나요? [화면 둘러보기](overview/screens.md) 포함 |
| [guides/](guides/install/installation-guide.md) | Dotori를 어떻게 운영하나요? |
| [reference/](reference/interfaces.md) | 명칭과 계약에 대한 설명 |
| [architecture/](architecture/README.md) | Dotori 설계 구조에 대한 이해 |

## 가이드 목록

| 하고 싶은 일 | 문서 |
| --- | --- |
| 처음 파일을 올리고 검색하기 | [첫 문서로 시작하기](guides/use/first-document.md) |
| 백업하거나 버전 올리기 | [백업과 데이터 보관](guides/operate/backup-and-storage.md), [업데이트 가이드](guides/operate/upgrading.md) |
| 서버를 처음 설치하기 | [설치 가이드](guides/install/installation-guide.md), [LLM 설치 가이드](guides/install/llm-installation-guide.md) |
| 카탈로그에 없는 임베딩 모델을 등록하기 | [임베딩 설치 가이드](guides/install/embedding-installation-guide.md) |
| 운영하며 계정과 검색 설정을 다루기 | [운영 가이드](guides/operate/operation-guide.md) |
| 모델이나 설정을 바꾸기 | [설정 변경 적용](guides/operate/applying-changes.md), [임베딩 런타임 가이드](guides/operate/embedding-guide.md) |
| 시스템 품질을 확인하기 | [모니터링 가이드](guides/operate/monitoring-and-quality-guide.md) |
| 직접 연동하기 | [연동 가이드](guides/integrate/README.md), [API 계약](reference/api-contract-v1.md) |
