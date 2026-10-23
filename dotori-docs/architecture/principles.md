# 설계 원칙

> 이전: [아키텍처](./README.md) · 다음: [Core](./blocks/core.md)

이 문서는 Dotori가 지키는 원칙과 제약, 그리고 그것을 만족시키기 위해 고른 구조 패턴을 정의합니다. 쉬운 말로 요약한 것은 [Dotori란 무엇인가](../overview/what-is-dotori.md)에 있고, 여기서는 이후 문서가 인용하는 정밀한 정의를 둡니다.

## 원칙

### Full-Stack

웹 UI부터 백엔드, 데이터 처리, 검색, RAG 실행까지 필요한 모든 계층을 하나의 제품으로 제공합니다. 선택한 기능의 업로드부터 결과 접근과 장애 복구까지 제공한다는 뜻이며, RAG는 준비된 결과를 사용하는 기능 중 하나입니다.

### Modular

새 자료 유형을 추가할 때 기존 모듈의 처리 알고리즘을 수정하지 않습니다. 정적 등록, 설치 구성, API와 UI 조합의 추가는 허용합니다. 현재 코드와 DB는 공유되므로 각 모듈의 완전한 독립 배포를 보장하지는 않습니다.

### Lightweight

선택하지 않은 기능의 모델, 무거운 의존성, 실행 서비스를 요구하지 않습니다. 선택한 구성의 자원 사용량은 측정해 관리합니다.

### Self-Hosted

서비스를 직접 호스팅하고 제어할 수 있도록 설계합니다.

## 제약

원칙이 언제나 제약을 낳는 것은 아닙니다. Full-Stack과 Modular는 제품의 형태와 내부 구조를 규정하지만 선택지를 닫지는 않으므로, 제약이 아니라 [해결 전략](#해결-전략)에서 다룹니다. 각 항목은 진술과 함께 출처와 결과를 기술합니다. 하드웨어 자동 선택과 모델 중복 로드 방지는 제품이 채택한 정책이며 Self-Hosted 자체에서 자동으로 도출되는 규칙은 아닙니다.

### 서버는 운영자의 것입니다

서버가 운영자의 것이라는 선택은, 제품이 실행 환경과 데이터에 대해 가질 수 있는 권한을 제한합니다.

- 출처: 운영자의 서버는 제품이 통제할 수 없는 환경에서 동작합니다.
- 결과: 지원 환경과 CPU 경로를 명시합니다. 설치 또는 명시적인 모델 변경 때 하드웨어를 탐지하여 서버 전체의 모델, 컨텍스트, 동시 처리 수, 메모리, 양자화, 런타임을 결정합니다. 사용자별 모델 선택이나 LLM 우선순위 프리셋은 추가하지 않습니다.

### 로컬 모델을 중복해서 올리지 않습니다

최소한의 자원으로 동작해야 한다는 목표는, 상시 실행되는 구성요소의 수와 메모리 점유 방식을 제한합니다.

- 출처: 모델을 이중 로드하여 처리량을 늘리는 방식은 사용하지 않습니다.
- 결과: 단일 서버의 선택한 로컬 모델은 전용 실행 프로세스에서 로드하고 웹·오케스트레이터에서 다시 로드하지 않습니다. OCR, 임베딩, LLM처럼 서로 다른 모델은 함께 필요할 수 있습니다. 모델 프로세스 수와 동시 요청·배치 처리 수는 별개입니다.

### 모델 가중치는 제품 이미지와 분리합니다

- 출처: 배포 용량과 모델 라이선스.
- 결과: 온라인 설치는 선택한 가중치를 취득합니다. 오프라인 설치·운영은 필요한 이미지, 가중치, 설정을 사전 확보하고 반입하는 절차를 전제로 합니다. 가중치를 이미지에 포함하지 않는다는 이유만으로 오프라인 설치가 불가능한 것은 아닙니다.

### 운영자가 인프라 전문가가 아닙니다

- 출처: 대상 사용자층. 전담 인프라 담당자가 없는 개인 및 소규모 운영자입니다.
- 결과: 여러 단계의 수동 구성을 요구하는 설치 절차를 선택할 수 없습니다. 운영자에게 노출되는 선택지는 제품 수준의 용어로만 표현되어야 하며, 내부 구현 용어를 묻거나 동작에 영향을 주지 않는 선택지를 물어서는 안 됩니다.

### 운영자 접점의 기본 언어는 한국어입니다

- 출처: 대상 사용자층.
- 결과: 설치 프롬프트, 환경 변수 예시, 운영 문서가 한국어를 우선합니다. 코드와 API 식별자는 영어를 유지합니다.

## 해결 전략

### 아키텍처 분류와 적용 범위

전체 구조는 **모듈러 모놀리스 기반의 선택형 처리 제품**입니다. [Core–Orchestrator–Executor](./README.md#핵심-구조-접점실행-제어실제-처리)의 책임 분리는 제품 접점·실행 제어·실제 처리를 나누는 중심 전략입니다. 기능 모듈은 그 공통 기반에 연결되고 결과와 알고리즘을 소유합니다. 다음 패턴은 서로 다른 질문에 답하며 하나의 이름으로 섞지 않습니다.

- 모듈러 모놀리스는 코드와 상태의 조직을 설명합니다. 도메인 backend와 DB를 공유하고 소유 경계를 나눕니다. 무거운 처리 프로세스를 별도로 기동해도 모듈별 독립 DB·배포를 제공하는 마이크로서비스 구조는 아닙니다.
- Ports and Adapters는 구현 연결을 설명합니다. handler·repository·plan adapter·Provider 계약으로 공통 제어와 도메인 구현, 처리 계약과 라이브러리/모델을 연결합니다. 전체 Django 도메인이 ORM과 독립된 순수 헥사고날 구조라는 뜻은 아닙니다.
- 마이크로커널 원칙은 확장 방향을 설명합니다. 공통 기반에 정적으로 등록한 기능 모듈을 선택하고 domain 알고리즘을 바깥에 둡니다. 현재는 임의 package의 runtime hot-loading이나 코드·schema의 물리적 탈착을 제공하지 않습니다.
- 제어와 실행의 분리는 운영 책임을 설명합니다. 승인·명령·보고·재시도와 CPU/RAM/VRAM을 사용하는 실제 처리를 나눕니다. Orchestrator가 모든 자연어를 해석하는 agent나 범용 DAG 플랫폼이라는 뜻은 아닙니다.

공유 DB는 마이크로커널이나 Ports and Adapters의 필수 조건이 아니라 Dotori의 경량성과 운영 단순성을 위한 선택입니다. 새 모듈 추가 때 등록·설치·API/UI 조합의 수정은 허용하지만 기존 module 알고리즘과 공통 제어의 도메인별 분기 증가는 구분합니다. 현재 Documents만 등록되어 있으므로 두 번째 모듈의 실제 설치·처리·결과 접근으로 이 경계를 검증해야 합니다.

### 얻는 효과와 부담

접수와 권한을 무거운 추론에서 분리하고, 서로 다른 자료 유형의 공통 전달·상태 관리와 준비 결과의 여러 소비자를 재사용할 수 있습니다. 선택하지 않은 모델·서비스를 시작하지 않는 구성을 유지하고 로컬 AI와 외부 endpoint 연결을 같은 제품에서 지원합니다.

대신 승인·명령·receipt·보고·도메인 결과의 책임과 정합성을 관리해야 합니다. 공유 코드·DB는 version/schema 변경의 coordinated deployment를 요구하며, 내부 HTTP·broker 전달에는 실패와 재전달 경계가 있습니다. 서비스 수를 늘리지 않았다는 사실만으로 메모리·DB·지연 비용이 없다고 판단하지 않습니다. 모든 장애의 exactly-once 추론이나 사전 처리에 따른 생성 속도 향상을 보장하지 않습니다.

Dotori의 구조를 이해하는 데 세 가지 개념을 씁니다.

- 모듈러 모놀리스: 제품의 코드와 상태를 관리하는 관점
- 마이크로커널: 선택적 기능을 Core에 연결하여 기능과 목표의 확장을 가능하게 하는 구조
- DDD aggregate 패턴: 도메인 규칙을 관리하는 기준

Dotori 전체를 DDD 또는 완성된 마이크로커널로 분류하지는 않습니다. 외부 처리 구현의 교체에는 Ports and Adapters의 계약 분리 원칙을 일부 적용합니다. 실제 `DocumentParser`와 `EmbeddingProvider` Protocol이 그 예이며, 전체 도메인이 Django ORM과 독립된 구조는 아닙니다.

각 개념은 앞의 원칙과 제약에서 나온 선택입니다.

| 개념 | 나온 곳 |
| --- | --- |
| 모듈러 모놀리스 | Lightweight와 서버 한 대. 도메인 코드·DB를 공유하되 무거운 실행은 별도 프로세스에 격리합니다 |
| 마이크로커널 | Modular. 새 자료 유형을 추가할 때 기존 모듈을 고치지 않도록 기능을 Core 바깥의 모듈로 둡니다. 운영자가 인프라 전문가가 아니므로 기능은 설치할 때 정적으로 선택합니다 |
| Ports and Adapters(일부) | 모델과 파서 구현의 교체. 기능 모듈이 계약을 소유하고 구현이 그 계약을 따릅니다 |
| DDD aggregate(참고) | 블록의 쓰기 규칙. 함께 맞아야 하는 데이터의 범위를 정하는 기준입니다 |

마이크로커널 패턴의 구성과 그 담당은 다음과 같습니다.

| 구성 | 명칭 | 담당 |
| --- | --- | --- |
| Kernel | [Dotori Core](./blocks/core.md) | 사용자와 워크스페이스, 파일의 저장과 권한 |
| Registry | [Dotori Registry](./blocks/registry.md) | 플러그인이 선언할 수 있는 계약과 실행 가능 여부의 판정 |
| 기능 모듈 (Plugin) | [Documents](./blocks/documents.md), 향후 Images·Audio·Videos | 유형별 처리 알고리즘과 결과 소유 |
| 실행 프로세스 | *-executor ([배포](./deployment.md)) | 처리 구현을 모듈 코드로부터 분리하여 실행 |
| 구현 Adapter | [Provider](./crosscutting/provider.md)가 가리키는 DoclingParser, 임베딩 provider 등 | 처리 계약을 특정 라이브러리·모델·엔드포인트에 연결 |
| 작업 전달 | [Dotori Orchestrator](./blocks/orchestration.md) | 큐 소비, 내부 HTTP 전달, 재시도와 후속 작업 발행 |
| Installer | [Dotori Installer](./blocks/installation.md) | 어떤 플러그인과 어댑터를 둘지 결정하고 기록 |

작업의 명령과 전달은 제어 평면의 책임이며 각 모듈은 자기 작업을 그 계약으로 요청합니다. 정적으로 등록한 기능을 설치 시 선택하며 hot-loading은 요구하지 않습니다. 공유 DB 안에서도 유형별 테이블의 소유자는 구분할 수 있도록 합니다. 목표 구조에서 모듈 간 처리는 입력·결과 참조와 등록된 작업을 통해 연결합니다.

## 유사 시스템과 비교하는 관점

다음은 구조를 이해하기 위한 부분 비교이며 같은 아키텍처나 기능·성능의 우열을 뜻하지 않습니다.

- [Paperless-ngx 구조](https://github.com/paperless-ngx/paperless-ngx/blob/main/docs/usage.md#architecture)는 웹/API 접점, 파일 유입 consumer와 task processor를 구분합니다. 파일 접수와 비동기 처리를 비교할 수 있지만 consumer를 Dotori의 승인 계획·보고 제어 전체와 동일시하지 않습니다.
- [Onyx 배포 구성](https://github.com/onyx-dot-app/onyx/blob/main/deployment/docker_compose/docker-compose.yml)은 API·background 처리와 요청/색인용 모델 서버를 분리합니다. 모델 의존성 격리를 참고할 수 있습니다. Dotori는 현재 query/document embedding을 하나의 endpoint와 admission으로 공유하며 같은 실행 topology를 채택한 것은 아닙니다.
- [Airflow 아키텍처](https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/overview.html)는 workflow 제어와 task 실행을 구분합니다. 상태·의존성·재시도 책임을 비교할 수 있으나 Dotori는 파일 전처리 제품이며 범용 workflow 플랫폼이 아닙니다. Airflow의 Executor는 실행 전달 메커니즘이고 실제 처리자는 Worker이므로 Dotori의 Executor와 이름만으로 대응시키지 않습니다.

## 참고문헌

패턴 정의는 [Microkernel Architecture — Mark Richards](https://www.oreilly.com/content/software-architecture-patterns/), [DDD Aggregate — Martin Fowler](https://martinfowler.com/bliki/DDD_Aggregate.html), [Hexagonal Architecture — Alistair Cockburn](https://alistair.cockburn.us/hexagonal-architecture)를 참고합니다. 이는 이 문서의 용어와 설계 판단 기준이며 프로젝트의 역사적 설계 출처를 단정하지 않습니다.
