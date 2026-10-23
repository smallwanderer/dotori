# 등록부

> 이전: [Core](./core.md) · 다음: [오케스트레이션](./orchestration.md)

이 문서는 등록부(B6)를 자세히 다룹니다. 등록부는 설치와 실행 측이 함께 읽는 정적 선언을 소스 코드로 소유하며, 선언 영역은 Django ORM과 모델 추론 의존성을 가져오지 않습니다.

| 블록 | 소유하는 상태 | 코드 위치 |
| --- | --- | --- |
| B6 등록부와 판정 | 역량, 모듈, 작업 종류, 실행 역할의 선언 (코드 상수) | `app/capabilities/` |

## 선언 체인

네 선언은 모듈에서 실행 역할까지 한 줄로 엮입니다.

| 선언 | 답하는 것 |
| --- | --- |
| 모듈 (`modules.py`) | 설치 단위는 무엇이고 어떤 어댑터를 요구하는가 |
| 역량 (`registry.py`) | 그 모듈이 제공하는 기능과 그 의존 관계는 무엇인가 |
| 작업 종류 (`work_registry.py`) | 그 기능을 수행하는 작업이 어느 역할과 큐로 가는가 |
| 실행 역할 (`execution.py`) | 그 역할을 어떻게 호출하고 주소를 어디서 얻는가 |

```
모듈  documents
 ├ 역량  documents.extract
 │   └ 작업  parse_document           → 역할 parser    → 큐 parse
 └ 역량  documents.semantic_search    (extract에 의존)
     ├ 작업  embed_document           → 역할 embedding → 큐 embed (명시 승인 계획)
     ├ 작업  plan_document_embedding  → 역할 embedding → 큐 embed (기존 업로드 사슬)
     ├ 작업  embed_document_batch     → 역할 embedding → 큐 embed
     └ 작업  evaluate_retrieval       → 역할 embedding → 큐 embed
```

트리는 선언의 소속을 보여 줍니다. 작업 사이의 실행 순서와 후속 관계는 [처리 파이프라인](../runtime/pipeline.md)에서 다룹니다.

Function 계약과 work kind는 같지 않습니다. schema 4 배포 snapshot은 document.extract·text.embed·text.generate의 후보와 model-free parser를 표현하며 구 schema 2/3 reader도 유지합니다. 실제 초기 계획·승인 명령은 [오케스트레이션](./orchestration.md)이 담당합니다. 관측의 자동 근거 적용과 전체 기능 의존성 판정은 아직 후속이며 선언만으로 품질 검증을 뜻하지 않습니다.

## 실행 역할

등록된 기능 모듈은 `documents` 하나입니다. 다음 실행 역할은 선택한 역량에 따라 필요성이 달라집니다. 추출에는 parser, semantic search에는 embedding이 필요합니다. LLM은 RAG 소비 경로에서 사용하며 역량 선언에 RAG는 별도로 없습니다.

| 실행 역할 | 호출 방식 | 설정 출처 |
| --- | --- | --- |
| parser | 오케스트레이터의 내부 HTTP | `DOCUMENT_PARSER_BACKEND` |
| embedding | 내부 HTTP 및 요청 경로의 직접 HTTP | `embedding_runtime.json` |
| llm | 요청 경로의 직접 HTTP | `llm_runtime.json` |

## Function 계약

Function 계약은 모델이나 구현이 제공하는 호출의 입출력을 데이터로 선언합니다. ID는 데이터 계약을 가리킬 뿐이며 실행할 코드의 import 경로가 아닙니다. 현재 세 개가 등록되어 있습니다.

| 함수 | 선호 역할 | 입력과 출력 | 허용되는 어댑터 계약 |
| --- | --- | --- | --- |
| `document.extract` | parser | 문서 원본에서 추출 결과 | `dotori-parser-http-v1` |
| `text.embed` | embedding | 텍스트에서 임베딩 | `dotori-internal-embed-v1` |
| `text.generate` | llm | 대화 메시지에서 완성문 | `openai-compatible-chat-completions-v1` |

배포 계획의 후보가 이 선언과 계약 버전, 스키마, 어댑터 계약이 하나라도 다르면 거부됩니다. 여기에 더해 `document.embed`는 `text.embed`를 감싸 문서 단위 임베딩을 하는 합성 계약으로 따로 선언합니다. 합성 계약은 모델이 직접 제공하는 함수처럼 설치 계획에 광고되지 않으며, 이 선언만으로 어떤 작업 경로도 열리지 않습니다.

## 알려진 구현 이름

등록부는 출하되는 임베딩 구현의 이름을 코드 상수로 가집니다(`bgem3_hybrid`, `sentence_transformers`, `openai_compatible`). 문서 블록이 구현 레지스트리를 만들 때 이 이름 집합과 같은지 대조하고, 하나라도 더하거나 빠지면 거부합니다. 이름을 한쪽에만 더할 수 없게 하려는 장치이며, 이름이 구현의 동작을 보장하지는 않습니다([Provider](../crosscutting/provider.md#이름이-하는-일)). parser는 `docling` 하나이고, 이 등록 외의 파서 제공자는 받지 않습니다. 플러그인을 찾아 불러오는 구조가 아닙니다.

## 배포 계획 snapshot

등록부는 활성 배포 계획(`deployment_plan.json`)을 읽어 검증된 읽기 전용 snapshot으로 돌려줍니다. 선택된 모듈과 설치된 역량, 켜진 역량, 역할 바인딩, 실행 역할, 서비스, 큐, 작업 경로, 설치된 모델과 함수 제공자를 담습니다.

- 현재 계획의 스키마는 4이며 2와 3도 읽습니다. 지원하지 않는 버전이나 필수 필드가 없는 계획은 읽을 때 거부합니다.
- 읽을 때 선언과 어긋나는 내용을 모두 검사합니다. 예를 들어 서버에는 임베딩 모델이 하나만 설치될 수 있고, 기본 경로가 가리키는 제공자는 검증을 통과한 후보여야 합니다.
- 한 번 읽은 snapshot은 프로세스 안에 캐시됩니다. 계획을 바꾼 뒤 서비스를 다시 시작하는 이유가 이것입니다([설정 변경 적용](../../guides/operate/applying-changes.md)).

## 기능 활성 판정

어떤 역량이 켜져 있는지는 snapshot이 답합니다. 요청 경로와 작업 생산 경로 모두 같은 판정을 거치며, `require_capability`는 거부 사유를 세 가지로 구분하고, 작업 경로의 등록까지 확인하는 발행 경로는 `CAPABILITY_UNAVAILABLE`을 더합니다([기능 활성](../crosscutting/activation.md#거부-사유)).

| 사유 | 의미 |
| --- | --- |
| `CAPABILITY_CONFIG_INVALID` | 배포 계획을 읽지 못했거나 검증에 실패했습니다 |
| `CAPABILITY_NOT_INSTALLED` | 활성 배포에 그 역량이 설치되어 있지 않습니다 |
| `CAPABILITY_DISABLED` | 설치되어 있지만 이 서버에서 꺼져 있습니다 |

단순히 켜짐 여부만 묻는 `capability_enabled`는 계획을 읽지 못하면 꺼진 것으로 봅니다. 규칙은 [기능 활성](../crosscutting/activation.md)이 다룹니다.

## 입력 라우팅

업로드된 파일이 어떤 초기 작업이 되는지도 등록부가 정합니다. 파일 이름의 확장자로 입력 종류를 판정하고(현재는 `document` 하나이며 텍스트, 코드, 오피스 문서, HWP, 이미지 확장자를 포함합니다), 입력 종류와 작업을 잇는 기본 연결이 `document`를 `parse_document`에 묶습니다. 이후의 임베딩은 업로드 시점에 발행하지 않고 파싱이 끝난 뒤 문서 워크플로가 계획합니다([처리 파이프라인](../runtime/pipeline.md)).

| 상황 | 결과 |
| --- | --- |
| 알려진 확장자이고 역량이 켜져 있음 | `parse_document` 작업 하나 |
| 알려진 입력이지만 그 역량이 꺼져 있음 | 작업 없음. 오류가 아닙니다 |
| 등록되지 않은 확장자 | `UNSUPPORTED_INPUT` |
| 입력 종류에 연결된 작업이 없음 | `NO_ACTIVE_HANDLER` |
| 연결된 작업이 등록되지 않았거나 그 입력을 받지 않음 | `WORK_NOT_REGISTERED`, `INPUT_ROUTE_MISMATCH` |

## 실행 기능과의 경계

공통 실행 기능은 `app/orchestration/`에 있으며 `capabilities/work_dispatch.py`는 호환 재수출 경로입니다. 선언과 Django·Celery를 사용하는 실행 기능의 의존 경계는 [블록 경계 규칙](../crosscutting/boundaries.md)의 검사로 확인합니다.
