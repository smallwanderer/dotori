# Dotori 문서

Dotori는 운영자가 가진 파일을 미리 처리해 두고, 준비된 결과를 검색과 질의, 외부 AI 클라이언트가 다시 쓰게 하는 셀프호스팅 제품입니다. 문서는 위에서 아래로 갈수록 깊어지므로 필요한 깊이에서 멈추면 됩니다.

## 어디서 시작하나요

| 읽는 사람 | 알고 싶은 것 | 시작점 |
| --- | --- | --- |
| 운영자 | 설치와 운영, 어떤 구성으로 동작하는지 | [Dotori란 무엇인가](overview/what-is-dotori.md) → [동작 방식](overview/how-it-works.md) → [설치 가이드](guides/install/installation-guide.md) |
| 통합 개발자 | API, CLI, 동기화 클라이언트로 연동하는 방법 | [동작 방식](overview/how-it-works.md) → [연동 가이드](guides/integrate/README.md) → [API 계약](reference/api-contract-v1.md) |
| 기여자·유지보수자 | 구조를 이해하고 바꾸는 방법 | [동작 방식](overview/how-it-works.md) → [아키텍처](architecture/README.md) |

## 폴더 구성

| 폴더 | 내용 | 깊이 |
| --- | --- | --- |
| [overview/](overview/what-is-dotori.md) | Dotori가 무엇이고 어떻게 동작하는지. [화면 둘러보기](overview/screens.md) 포함 | L0, L1 |
| [guides/](guides/install/installation-guide.md) | 작업 하나당 문서 하나. install(설치), operate(운영), integrate(연동) | 방법 |
| [reference/](reference/interfaces.md) | 정확한 이름과 계약을 찾아보는 자료 | 조회 |
| [architecture/](architecture/README.md) | 내부 구조와 설계 이유 | L2, L3 |

## 가이드 목록

| 하고 싶은 일 | 문서 |
| --- | --- |
| 서버를 처음 설치한다 | [설치 가이드](guides/install/installation-guide.md), [LLM 설치 가이드](guides/install/llm-installation-guide.md) |
| 카탈로그에 없는 임베딩 모델을 등록한다 | [임베딩 설치 가이드](guides/install/embedding-installation-guide.md) |
| 운영하며 계정, 파이프라인, 검색 설정을 다룬다 | [운영 가이드](guides/operate/operation-guide.md) |
| 모델이나 설정을 바꾼 뒤 적용한다 | [설정 변경 적용](guides/operate/applying-changes.md), [임베딩 런타임 가이드](guides/operate/embedding-guide.md) |
| 상태와 검색 품질을 확인한다 | [모니터링 가이드](guides/operate/monitoring-and-quality-guide.md) |
| 직접 연동한다 | [연동 가이드](guides/integrate/README.md), [API 계약](reference/api-contract-v1.md) |

문서를 추가하거나 고칠 때의 규칙은 [문서 작성 규칙](architecture/extending/writing-docs.md)에 있습니다.
