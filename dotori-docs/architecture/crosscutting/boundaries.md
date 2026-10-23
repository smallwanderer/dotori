# 블록 경계 규칙

> 이전: [설계 원칙](../principles.md) · 다음: [작업 상태와 정합성](./consistency.md)

이 문서는 블록 사이에 지켜야 할 쓰기 규칙과 참조 방향, 그리고 이를 검사하는 테스트를 설명합니다.

## 쓰기 규칙

블록은 자기 상태만 씁니다. 다른 블록의 상태 변경은 소유 서비스를 통해 요청하고, 읽기도 공개 서비스와 read adapter를 우선하여 내부 ORM 관계에 대한 종속을 줄입니다. 새 자료 유형을 모듈로 추가할 때 기존 모듈을 수정하지 않으려면 각 모듈이 자기 상태에 닫혀 있어야 하기 때문입니다([Modular](../principles.md#modular)).

## 참조 방향

기반 블록은 기능 모듈을 알지 않습니다. B5, B6, B7은 B4를 참조하지 않고, Core(B1~B3)도 진입점을 제외하고는 참조하지 않는 것을 원칙으로 합니다. 기반이 기능 모듈을 알면 모듈을 추가할 때마다 기반을 수정해야 하기 때문입니다([해결 전략](../principles.md#해결-전략)). 현재의 예외는 [차이와 과제](../gaps.md)에 있습니다.

## 일관성 경계

공유 DB의 제약, 잠금, 트랜잭션은 코드 경계와 함께 일관성을 보장하는 수단입니다. 트랜잭션 범위는 함께 보장해야 할 불변식에서 정하며 aggregate로 이름 붙였다는 이유만으로 분리하지 않습니다. 여러 소유 서비스의 변경을 공유 transaction으로 조합할 경우 불변식과 잠금 순서를 명시합니다. 예를 들어 파싱 저장 서비스는 결과와 청크 교체를 한 transaction으로 처리하고, RAG 대화 예약 서비스는 대화를 잠그고 메시지와 작업을 함께 생성합니다. DB transaction은 파일시스템 삭제나 broker 발행을 rollback하지 못합니다.

DDD의 aggregate는 변경이 루트를 통과하고 불변식을 유지하는 단위를 검토할 때 참고합니다. 전체 코드가 이 규칙을 강제하지 않으므로 앱이나 테이블 묶음을 곧바로 aggregate로 부르지 않으며, 파일 변경과 Workspace quota의 일관성 범위를 구분합니다([해결 전략](../principles.md#해결-전략)).

## 검사

`app/tests/ops/test_block_boundaries.py`가 두 규칙을 검사합니다. import 검사는 `ast.walk`로 함수 안쪽의 import까지 찾습니다. 지금까지의 위반은 모듈 최상단이 아니라 함수 안에 숨어 있었기 때문입니다.

| 규칙 | 검사 | 한계 |
| --- | --- | --- |
| `llm_installation`(B5)은 `documents`를 참조하지 않습니다 | 해당 패키지의 모든 import | 최상위 패키지 이름으로 비교하며 동적 import(`importlib`)는 보지 못합니다 |
| `capabilities`(B6), `orchestration`과 `orchestration_state`(B7)는 `documents`와 `llm_installation`을 참조하지 않습니다 | 세 패키지의 모든 import | |
| `workspaces/api_v1/views.py`의 `documents` 의존은 모듈 선언부에 드러납니다 | 함수 안쪽 import를 금지합니다 | 이 한 파일의 예외만 다룹니다 |
| 블록은 다른 블록의 모델을 쓰지 않습니다 | `Model.objects` 아래의 `create`, `update`, `delete`, `bulk_create`, `bulk_update`, `update_or_create`, `get_or_create` 호출을 모델 이름으로 소유 블록과 대조합니다 | 인스턴스 `save()`, 관련 매니저, 원시 SQL은 보지 못합니다. 두 블록이 같은 이름을 쓰는 모델은 건너뜁니다 |

쓰기 검사에는 양성 대조가 있습니다. 같은 블록 안의 쓰기가 한 건도 감지되지 않으면 검사 자체가 실패합니다. 감지기가 아무것도 보지 못해서 통과하는 일을 막기 위해서입니다. 읽기는 의도적으로 제한하지 않습니다. 문서 블록이 Files와 Workspaces를 읽는 것이 그 예입니다.

이 검사는 하한선입니다. 블록 사이 참조를 전면 금지하지 않으며, 검사가 보지 못하는 쓰기는 [아키텍처](../README.md)의 블록 표가 리뷰 기준이 됩니다.
