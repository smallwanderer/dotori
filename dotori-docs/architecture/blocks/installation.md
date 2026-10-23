# 설치와 런타임

> 이전: [문서](./documents.md) · 다음: [카탈로그](./catalog.md)

이 문서는 설치와 런타임(B5)을 자세히 다룹니다. B5는 설치 절차와 배포 계획, runtime 설정을 만들고 읽습니다.

| 블록 | 소유하는 상태 | 코드 위치 |
| --- | --- | --- |
| B5 설치와 런타임 | 설치 계획, 런타임 설정 (호스트의 JSON 파일) | `app/llm_installation/`, `installation/`, `install.py`, `llm-runtime/` |

B5는 서버를 설치하고 모델을 바꾸는 시점에 하드웨어를 살피고, 무엇을 켤지 고르고, 그 결과를 호스트의 JSON 파일로 남깁니다. 실행 중인 서비스는 그 파일을 읽어 동작합니다.

## 두 종류의 파일

| 파일 | 정하는 것 | 쓰는 곳 | 읽는 곳 |
| --- | --- | --- | --- |
| 설치 계획 `deployment_plan.json` | 어떤 기능과 실행 역할과 서비스가 켜지는가 | `installation/deployment.py` | 설치기와 기능 활성 판정 |
| LLM 런타임 설정 `llm_runtime.json` | 어떤 LLM을 어떤 값으로 서빙하는가 | `llm_installation/config_store.py` | 요청 경로의 `rag_runtime_config.py` |
| 임베딩 런타임 설정 `embedding_runtime.json` | 어떤 임베딩 모델과 Provider와 차원을 쓰는가 | `llm_installation/embedding_config_store.py` | 문서 블록의 임베딩 설정 reader |

설치 계획은 켜짐과 꺼짐을, 런타임 설정은 모델과 실행 값을 정합니다. 내용을 서로 복제하지 않습니다. 파일의 위치는 [데이터 모델](../../reference/data-model.md#호스트-파일과-선언-상수)에 있습니다.

## 코드 구성

| 구분 | 위치 | 하는 일 |
| --- | --- | --- |
| 설치 진입점 | `install.py` | 설치 마법사와 `--restart`, `--change-llm`, `--change-embedding` 같은 운영 명령을 받습니다 |
| 배포 계획 | `installation/deployment.py` | 설치 모드에서 서비스 구성(`DeploymentPlan`)을 만들고 파일로 씁니다 |
| 외부 접속 | `installation/network_access/` | HTTPS 리버스 프록시 설정 파일을 만들고 연결합니다 |
| 하드웨어 탐지 | `llm_installation/runtime_probe.py`, `installer_adapter.py` | GPU와 메모리, Docker 서비스를 살핍니다 |
| 선택과 계획 | `llm_installation/selection.py`, `planner.py`, `calibration.py` | 카탈로그에서 모델을 고르고 메모리를 추정해 서빙 계획과 동시 처리 수를 정합니다 |
| 설정 기록 | `llm_installation/config_store.py`, `embedding_config_store.py` | 런타임 설정을 세대 단위로 쓰고 확정합니다 |
| 런타임 수명주기 | `llm_installation/runtime_lifecycle.py` | LLM 컨테이너를 빌드, 기동, 검증, 교체합니다 |
| 요청 경로 reader | `llm_installation/rag_runtime_config.py`, `router.py` | 확정된 LLM 설정을 읽어 요청이 쓸 대상을 정합니다 |

설치 시점에만 쓰는 코드와 요청 경로에서도 읽는 코드는 구분됩니다. 요청 경로에서 읽는 쪽(`rag_runtime_config.py`)은 모델이나 추론 라이브러리를 가져오지 않고 설정 파일만 읽습니다. 확정된 대상은 프로세스 안에서 캐시되므로 설정을 바꾼 뒤에는 서비스를 다시 시작해야 합니다([설정 변경 적용](../../guides/operate/applying-changes.md)). B5는 B4(`documents`)를 참조하지 않으며, 이 규칙은 [블록 경계 규칙](../crosscutting/boundaries.md)의 검사로 확인합니다.

## LLM 설치의 흐름

```mermaid
flowchart LR
    A["하드웨어 탐지"] --> B["카탈로그에서<br/>모델 선택"]
    B --> C["메모리 추정과<br/>서빙 계획"]
    C --> D["런타임 설정을<br/>세대로 기록"]
    D --> E["컨테이너 빌드와 기동"]
    E --> F{"헬스 검증"}
    F -- "통과" --> G["활성 설정으로 확정"]
    F -- "실패" --> H["이전 컨테이너 복원"]
```

설정은 곧바로 활성 파일에 쓰지 않습니다. 먼저 `runtime_scopes/<범위>/generations/<세대 id>/` 아래에 세대로 기록하고, 컨테이너가 헬스와 엔드포인트 검증을 통과한 뒤에야 활성 설정 파일을 임시 파일 쓰기와 교체(`os.replace`)로 갱신합니다. 그래서 검증에 실패해도 기존 활성 설정은 그대로 남습니다.

## 교체와 롤백

- **LLM 컨테이너**: 같은 이름의 컨테이너는 하나만 둘 수 있으므로, 기존 컨테이너를 멈추고 `<이름>-previous`로 이름을 바꿔 둔 채 새 컨테이너를 띄웁니다. 새 컨테이너가 헬스와 엔드포인트 검증을 통과하면 활성 설정을 확정하고 기존 컨테이너를 지웁니다. 실패하면 기존 컨테이너의 이름을 되돌리고 다시 시작합니다. Dotori가 관리하지 않는 같은 이름의 컨테이너가 있으면 건드리지 않고 중단합니다.
- **임베딩 런타임**: `python install.py --change-embedding`이 새 세대를 만들어 활성화하고 서비스를 다시 시작하며, 재시작이 실패하면 `rollback_embedding_runtime`으로 이전 세대로 되돌립니다.

## 두 워크로드의 메모리

한 서버에는 LLM 서빙 컨테이너와 임베딩 executor가 함께 올라갑니다. 둘은 서로 다른 시점에 크기가 정해지므로 어느 한쪽의 등록만으로는 상대가 이미 쓰는 메모리를 알 수 없습니다. `memory_reservations.py`가 런타임 범위 단위로 지금 잡혀 있는 메모리를 한곳에 기록해, 나중에 크기를 정하는 쪽이 먼저 잡힌 만큼을 뺄 수 있게 합니다. 이 기록은 두 런타임 설정 파일에 넣지 않고 따로 둡니다. 임베딩 설정 파일의 지문이 문서마다 저장되어 검색 결과를 거르는 데 쓰이므로, 필드를 하나 더하면 모든 지문이 바뀌어 기존 문서가 검색에서 빠지기 때문입니다.

다루지 않는 것: 선택 대상의 목록 구조는 [카탈로그](./catalog.md)가, 선택이 프로세스에 적용되는 시점은 [설정 변경 적용](../../guides/operate/applying-changes.md)이 다룹니다.
