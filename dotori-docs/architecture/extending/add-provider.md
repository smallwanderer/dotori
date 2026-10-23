# Provider 추가

> 이전: [Provider](../crosscutting/provider.md) · 다음: [검증 등급](../../reference/verification-grades.md)

이 문서는 새 구현이나 새 모델을 추가할 때 함께 변경해야 하는 지점을 나열합니다.

## 먼저 새 구현이 필요한지 판단합니다

새 모델마다 새 구현이 필요하지는 않습니다. 모델을 어떻게 호출하느냐가 기존 구현의 범위 안이면 카탈로그 파일만 더합니다.

| 상황 | 필요한 작업 |
| --- | --- |
| SentenceTransformer로 dense 벡터를 만들 수 있는 모델 | 모델 파일과 `sentence_transformers` 프로필을 더합니다. 코드 변경은 없습니다 |
| OpenAI 임베딩 요청·응답 규약을 따르는 endpoint | `openai_compatible` 프로필을 더합니다. 코드 변경은 없습니다 |
| BGE-M3처럼 dense와 sparse를 함께 쓰는 모델 | 기존 `bgem3_hybrid`를 씁니다 |
| 위 호출 방식으로 부를 수 없는 임베딩 모델 | 새 구현을 코드로 더합니다 (아래) |
| 새 파서 | 코드와 선언을 함께 바꿉니다 (아래) |

선택 기준과 프로필 옵션은 [임베딩 Provider 선택과 후보 검증](../../guides/operate/embedding-provider-validation.md), 파일 작성법은 [임베딩 설치 가이드](../../guides/install/embedding-installation-guide.md)에 있습니다.

## 임베딩 구현을 더할 때

이름은 한 곳에만 더하면 안 되고, 다음 지점을 함께 바꿔야 합니다.

1. **구현 클래스**: `documents/embedding/providers/`에 `EmbeddingProvider` 계약(`spec`, `embed_document`, `embed_query`, `embed_documents`, `healthcheck`)을 따르는 클래스를 만들고, 클래스에 구현 이름(`backend`)을 둡니다.
2. **이름의 선언**: `capabilities/providers.py`의 `EMBEDDING_BACKENDS`에 이름과 `text.embed`를 더합니다. 카탈로그가 받아들이는 이름의 집합(`KNOWN_PROVIDERS`)이 여기서 만들어집니다.
3. **구현 레지스트리**: `documents/embedding/registry.py`의 `_PROVIDER_FACTORIES`에 클래스를 더합니다. 2번과 이름의 집합이 다르면 import할 때 오류가 나므로 한쪽만 고칠 수 없습니다.
4. **옵션**: 구현이 프로필 옵션을 받으면 `llm_installation/embedding_catalog/provider_options.py`에 옵션 모델과 검증 분기를 더합니다. 더하지 않으면 그 구현은 빈 옵션만 받습니다.
5. **프로필**: 새 프로필의 `provider`에 이름을 적고, 처음에는 `availability`를 `experimental`로 둡니다. `supported`로 올리려면 알려진 이름이고 저장소(`store`)의 차원이 프로필의 차원과 같아야 합니다([카탈로그](../blocks/catalog.md#선언이-통과하는-검사)).
6. **의존성**: 새 라이브러리가 필요하면 embedding 이미지의 의존성에 더하고 이미지를 다시 빌드합니다.

## 파서를 더할 때

파서는 현재 `docling` 하나만 받도록 여러 곳에 고정되어 있습니다. 플러그인을 찾아 불러오는 구조가 아니므로 다음 지점을 모두 확장해야 합니다.

- 구현: `DocumentParser` 계약(`spec`, `parse`, `healthcheck`)을 따르는 클래스를 만들고 `documents/parsers/registry.py`의 `_PARSER_FACTORIES`에 모듈 경로와 클래스 이름을 더합니다. 선택된 파서만 호출할 때 import되므로 레지스트리를 조회해도 무거운 의존성이 로드되지 않습니다.
- 선언: `capabilities/functions.py`의 `validate_provider_registration`과 `capabilities/modules.py`의 모듈 요구, 설치 쪽 `installation/deployment.py`의 배포 계획 작성이 모두 `parser:docling:1`로 고정되어 있습니다.
- 선택: `.env`의 `DOCUMENT_PARSER_BACKEND`로 고릅니다([설정 항목](../../reference/configuration.md)).

## 생성(LLM) 대상을 더할 때

생성에는 Protocol도 구현 레지스트리도 없습니다. 경우가 세 가지입니다.

- **다른 모델**: artifact 파일을 더합니다. 같은 실행 프로필(`backend_profiles`의 `llamacpp-cpu` 등)을 쓰면 코드 변경이 없습니다.
- **새 서빙 엔진**: 런타임 수명주기(`llm_installation/runtime_lifecycle.py`)와 `llm-runtime/`의 이미지 정의를 함께 바꾸는 코드 변경입니다.
- **외부 endpoint**: OpenAI 호환 chat completions 규약을 따르면 운영자가 endpoint로 등록할 수 있고 코드는 필요 없습니다. 전용 규약의 서비스는 호환 gateway가 필요합니다.

## 더한 뒤 확인할 것

확인한 것의 수준을 구분해 기록합니다. 로딩이 된다는 것과 계약을 지킨다는 것과 검색 품질이 좋다는 것은 서로 다른 주장입니다([검증 등급](../../reference/verification-grades.md)).

1. 설정 검사: `validate_embedding_candidate ... --config-only`
2. 실행 검사: 일회성 컨테이너에서 같은 명령으로 실제 모델을 불러 출력 차원, 정규화, sparse 선언, 토큰 제한을 확인합니다
3. 연결 검사: `--parser-report`와 함께 문서 → 청크 → 임베딩 연결을 확인합니다
4. 품질 검사: 검색 품질은 별도의 평가로 확인합니다

명령과 해석은 [임베딩 Provider 선택과 후보 검증](../../guides/operate/embedding-provider-validation.md)과 [Parser와 임베딩 후보의 연결 검증](../../guides/operate/parser-embedding-candidate-validation.md)에 있습니다.

## 기존 벡터와의 호환

구현, 모델, 옵션, 차원, prefix를 바꾸면 런타임 지문이 바뀝니다. 이 지문은 문서마다 저장되어 검색 결과를 거르는 데 쓰이므로, 지문이 다른 기존 벡터는 새 질의와 비교되지 않습니다. 따라서 모델이나 구현을 바꾸면 기존 문서를 다시 임베딩해야 하는 경우가 많습니다. 판단과 절차는 [임베딩 런타임 가이드](../../guides/operate/embedding-guide.md)가 다룹니다.

## 기록 양식

추가한 뒤 PR이나 변경 기록에 다음을 적습니다.

- 구현 이름, 모델과 revision, 프로필 id
- 바꾼 지점(위 목록에서 해당하는 것)
- 확인한 수준(설정, 실행, 연결, 품질)과 확인하지 않은 수준
- 기존 벡터와의 호환 여부와 재임베딩 필요 여부

다루지 않는 것: 카탈로그와 프로필 파일의 필드 작성법은 [임베딩 설치 가이드](../../guides/install/embedding-installation-guide.md)가, 카탈로그와 프로필의 역할 구분과 선택 구조의 근거는 [Provider](../crosscutting/provider.md)가 다룹니다.
