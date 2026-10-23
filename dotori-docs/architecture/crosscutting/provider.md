# Provider

> 이전: [등록부](../blocks/registry.md) · 다음: [Provider 추가](../extending/add-provider.md)

Provider는 [설계 원칙](../principles.md#해결-전략)의 구현 Adapter 가운데 무엇을 사용할지 가리키는 이름입니다. 이 문서는 그 이름이 무엇을 결정하고 어디에 선언되며 왜 이런 구조인지 설명합니다.

## Provider란 무엇인가

임베딩 카탈로그의 프로필 파일 한 줄을 예로 봅니다.

```json
{ "model_id": "bge-m3", "provider": "bgem3_hybrid", "dimension": 1024 }
```

`provider` 값이 이 모델을 어떤 구현으로 실행할지 정합니다. 운영 화면과 운영 문서에서는 "프로바이더", 환경 변수와 일부 코드에서는 `backend`라고 부르지만 모두 같은 것입니다.

Dotori는 파서나 임베딩처럼 바꿔 쓸 수 있는 처리를 계약과 구현으로 나눕니다. 기능 모듈이 계약(Protocol)을 정하고, 그 계약을 따르는 클래스가 구현이며, Provider는 그 구현을 가리키는 이름입니다. 이 분리는 Ports and Adapters를 일부 적용한 것으로, 계약이 Port이고 구현이 Adapter입니다. Provider는 실행 역할(parser, embedding, llm) 단위로 정의됩니다.

| 실행 역할 | Port | Provider |
| --- | --- | --- |
| parser | `DocumentParser` | `docling` |
| embedding | `EmbeddingProvider` | `bgem3_hybrid`, `sentence_transformers`, `openai_compatible` |
| llm | 없음 | 선택된 llama.cpp 또는 vLLM 런타임, 등록된 외부 endpoint |

현재는 `documents` 모듈이 세 역할을 모두 요구하므로 위 세 줄이 전부입니다. 생성(llm)에는 Protocol도 구현 레지스트리도 없어서 `provider` 필드는 임베딩 카탈로그에만 있습니다. 임베딩 벡터를 저장하는 `EmbeddingStore`도 같은 방식의 Port이지만 Provider는 아닙니다. 역할별 호출 방식과 설정 출처는 [등록부](../blocks/registry.md#실행-역할)의 실행 역할 표를 따릅니다.

### 이름이 하는 일

Provider는 이름이며, 이름은 세 가지 일을 합니다. 어떤 모듈과 클래스를 import할지 결정하고, 저장된 결과가 어떤 구성에서 나왔는지 남기며, 구현의 spec과 runtime 설정과 배포 계획과 응답이 같은 이름을 말하는지 검사합니다.

하지 않는 일은 하나입니다. 그 구현이 실제로 무엇을 하는지는 검증하지 않습니다. 임베딩 구현의 등록 검사는 이름의 집합이 일치하는지만 보고, 요청 시 일치 검사도 선언된 값끼리 비교합니다. 동작의 보장은 [검증 등급](../../reference/verification-grades.md)이라는 별도 축에서 다룹니다.

운영자는 Provider를 직접 고르지 않습니다. 임베딩은 프로필을 고르면 프로필이 Provider를 정하고, 외부 엔드포인트를 연결하면 `openai_compatible`이 사용됩니다. parser는 `.env`의 `DOCUMENT_PARSER_BACKEND`로 정하며 현재 값은 `docling` 하나입니다.

## 왜 이 구조인가

| 제약 | 그래서 생긴 구조 |
| --- | --- |
| [로컬 모델을 중복 로드하지 않는다](../principles.md#로컬-모델을-중복해서-올리지-않습니다) | 실제 구현의 생성을 모델 소유 프로세스로 제한합니다. 다른 프로세스는 전달 Client만 가집니다 |
| [모델 가중치를 제품 이미지와 분리한다](../principles.md#모델-가중치는-제품-이미지와-분리합니다) | 모델이 코드에 없습니다. 카탈로그와 runtime 설정이 모델을 지정합니다 |
| [운영자가 인프라 전문가가 아니다](../principles.md#운영자가-인프라-전문가가-아닙니다) | 운영자에게 Provider를 묻지 않습니다. 정적으로 등록된 구현 중에서 프로필이 정합니다 |
| [서버를 직접 호스팅한다](../principles.md#서버는-운영자의-것입니다) | 모델은 서버 단위로 하나입니다. 사용자별 모델 설정을 두지 않습니다 |
| [새 자료 유형을 모듈로 추가한다](../principles.md#modular) | 구현 교체를 Protocol 계약으로 분리합니다 |

채택하지 않은 것도 같은 제약에서 나옵니다. 모든 역할을 포괄하는 Provider 기본 클래스, 범용 `execute(dict)` 진입점, 패키지를 자동 발견하는 plugin loader를 두지 않습니다. 구현은 역할마다 소수이고 설치 시점에 확정되므로, 공통 추상을 유지하는 비용이 그 추상이 줄이는 비용보다 큽니다.

## 구조

### 선언이 놓인 곳

Provider는 상태를 소유하지 않으므로 빌딩 블록이 아닙니다. 선언은 세 블록에 나뉘어 놓입니다.

| 놓인 곳 | 선언하는 것 |
| --- | --- |
| 등록부 (B6, `app/capabilities/`) | 실행 역할, 모듈이 요구하는 Provider, Function 계약, 알려진 임베딩 구현 이름 |
| 기능 모듈 (B4, `app/documents/`) | Protocol, 구현 클래스, 이름을 클래스로 연결하는 구현 레지스트리 |
| 설치와 런타임 (B5) | 카탈로그, 배포 계획(어떤 역할이 활성인가), runtime 설정(확정된 값) |

Function은 입출력 스키마와 허용되는 어댑터 계약을 선언하는 데이터 계약입니다. 어댑터 계약은 `dotori-parser-http-v1`처럼 호출 규약을 가리키는 이름이며 Python import 경로가 아닙니다. 현재 `document.extract`, `text.embed`, `text.generate` 세 개가 등록되어 있습니다. Protocol이 코드의 계약이라면 Function은 데이터의 계약이어서, 계약은 두 겹입니다.

레지스트리는 클래스 객체 대신 모듈 경로와 클래스명을 등록하고 선택된 구현만 호출 시점에 import합니다. 레지스트리를 조회하는 것만으로 Docling이나 Torch가 로드되지 않습니다.

### 카탈로그와 프로필

임베딩의 선택은 카탈로그에서 이뤄집니다. 모델 파일은 저장소와 리비전, 토크나이저처럼 모델 자체를 적고, 프로필 파일은 그 모델을 어떤 구현으로 실행할지와 차원, 저장소, prefix, 정규화를 적습니다. 구현 이름은 프로필에 있어서 같은 모델을 서로 다른 구현의 프로필로 여러 개 둘 수 있습니다.

지원 대상(`supported`) 프로필에 적을 수 있는 이름은 코드에 등록된 것으로 한정됩니다. 이름마다 선언할 수 있는 값의 범위도 정해져 있어서, sparse를 만들지 않는 구현에 sparse 지원을 적거나 정규화를 요구하는 구현에 비정규화를 적으면 거부됩니다. 이름이 구현의 동작을 보장하지는 않지만 선언의 범위는 제한합니다.

생성 쪽 카탈로그는 모양이 다릅니다. 모델과 artifact를 짝지어 어떤 양자화와 파일로 배포할지를 정하며, 실행 대상인 llama.cpp 또는 vLLM 런타임은 설치 시 하드웨어 탐지로 결정됩니다. 카탈로그의 구조는 [카탈로그](../blocks/catalog.md)에서, 파일 형식과 필드는 [임베딩 설치 가이드](../../guides/install/embedding-installation-guide.md)에서 다룹니다.

### 예시: 임베딩 한 번

```text
설치   카탈로그에서 프로필 선택 → runtime 설정 기록
요청   app → 전달 Client → embedding-executor 내부 HTTP
실행   executor가 runtime 설정으로 실제 구현 생성 → 모델 또는 외부 API
```

`get_embedding_client()`는 전달 Client를 반환하고, `create_embedding_implementation()`은 모델 소유 프로세스에서만 실제 구현을 만듭니다. 문서 임베딩의 큐 경로와 검색 질의의 직접 호출 경로는 [처리 파이프라인](../runtime/pipeline.md)과 [검색과 RAG](../runtime/query.md)에서 다루며, 두 경로는 같은 executor와 같은 admission을 공유합니다.

전달 Client는 자신의 설정과 활성 runtime, 배포 경로가 일치하는지 확인하고 응답의 정체성도 검증합니다. 모델 변경 후 오래된 Client가 다른 설정의 벡터를 섞는 것을 막기 위해서입니다.

## 알아두어야 할 것들

- 선택은 설치 또는 명시적 변경 때 기록되고, 프로세스가 재시작될 때 적용됩니다. 변경별 방법은 [설정 변경 적용](../../guides/operate/applying-changes.md)에서 다룹니다.
- 모델을 바꾸면 기존 벡터와의 호환을 판단해야 하며 재임베딩이 필요한 경우가 있습니다.
- 파서 구현은 현재 `docling` 하나이며, 텍스트·일반 문서·HWP 경로를 함께 감쌉니다.
- 외부 임베딩 API를 쓰는 구성에서도 요청은 executor를 경유합니다. 로컬 가중치를 로드하지 않을 뿐입니다.
- Capability(역량) ID `documents.extract`와 Function ID `document.extract`는 한 글자 차이지만 서로 다른 선언입니다.

## 관련 문서

- [아키텍처](../README.md): 전체 구조의 허브
- [카탈로그](../blocks/catalog.md): 선택 대상의 목록이 선언되는 구조
- [설정 변경 적용](../../guides/operate/applying-changes.md): 선택이 프로세스에 적용되는 시점
- [Provider 추가](../extending/add-provider.md): 새 구현이나 새 모델을 추가할 때
- [검증 등급](../../reference/verification-grades.md): 선언과 검증의 구분
- [임베딩 설치 가이드](../../guides/install/embedding-installation-guide.md): 모델과 프로필 등록
- [임베딩 런타임 가이드](../../guides/operate/embedding-guide.md): 런타임 전환과 재임베딩
- [임베딩 Provider 선택과 후보 검증](../../guides/operate/embedding-provider-validation.md), [Parser와 임베딩 후보의 연결 검증](../../guides/operate/parser-embedding-candidate-validation.md): 후보 검증
