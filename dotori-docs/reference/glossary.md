# 용어집

> 이전: [Dotori란 무엇인가](../overview/what-is-dotori.md)

이 문서는 Dotori 문서 전체에서 같은 뜻으로 쓰는 용어를 찾아보는 곳입니다. 뜻은 한두 문장으로 쓰고 자세한 설명은 링크한 문서가 다룹니다. "독자" 열은 그 용어를 알아야 하는 사람입니다. "전체"는 처음 읽는 사람도 만나는 말입니다.

## 한글 용어

가나다 순입니다.

| 용어 | 독자 | 뜻 | 자세한 문서 |
| --- | --- | --- | --- |
| 관리자 | 전체 | 계정에 staff 권한이 있는 사용자입니다. 운영 요약, 로그, 품질 화면을 볼 수 있습니다. 개인 호스팅에서는 보통 본인입니다 | [모니터링 가이드](../guides/operate/monitoring-and-quality-guide.md) |
| 기능 모듈 | 기여자 | Core 위에 얹는 처리 기능의 단위입니다. 현재는 `documents` 하나입니다 | [문서](../architecture/blocks/documents.md) |
| 기능 활성(admission) | 기여자 | 어떤 기능이 켜져 있어 요청이나 작업을 받을 수 있는지 서버가 판정하는 일입니다 | [기능 활성](../architecture/crosscutting/activation.md) |
| 등록부 | 기여자 | 어떤 기능과 작업이 있고 어느 실행 역할로 가는지를 코드 상수로 선언해 둔 곳입니다 | [등록부](../architecture/blocks/registry.md) |
| 명시적 계획 | 기여자 | 운영자가 명령으로 접수하는 승인된 작업 계획입니다. 계획이 먼저 저널에 저장된 뒤 발행되며, 현재는 문서 임베딩에 쓰입니다 | [처리 파이프라인](../architecture/runtime/pipeline.md#명시적-계획-경로) |
| 배포 계획 | 운영자, 기여자 | 설치할 때 정해지는 계획입니다. 켜진 기능, 실행 역할, 작업 경로의 기준이 됩니다. 모델과 실행 값은 runtime 설정이 따로 갖습니다 | [기능 활성](../architecture/crosscutting/activation.md) |
| 벡터(dense, sparse) | 전체 | 문서 조각과 질문을 숫자로 바꾼 결과입니다. dense는 의미를 고정 길이로 담고, sparse는 단어별 가중치를 담습니다 | [검색과 RAG](../architecture/runtime/query.md) |
| 블록 | 기여자 | 데이터를 쓸 수 있는 권한이 같은 코드를 묶은 단위입니다. B1~B7의 번호는 식별용입니다 | [아키텍처](../architecture/README.md) |
| 사용자 | 전체 | 계정으로 접속해 파일을 올리고 검색하고 질문하는 사람입니다 | [Dotori란 무엇인가](../overview/what-is-dotori.md) |
| 설치 모드 | 전체 | 설치할 때 고르는 구성입니다. 기본, Hybrid/Search AI, Full 로컬 AI RAG가 있습니다 | [동작 방식](../overview/how-it-works.md#설치-모드에-따라-무엇이-켜지나요) |
| 실행 역할 | 기여자 | 작업을 실행하는 책임의 종류입니다. parser, embedding, llm이 있습니다 | [등록부](../architecture/blocks/registry.md#실행-역할) |
| 역량 | 기여자 | 모듈이 제공하는 기능의 선언입니다. 추출(`documents.extract`)과 의미 검색(`documents.semantic_search`)이 있고 의미 검색은 추출에 의존합니다 | [등록부](../architecture/blocks/registry.md#선언-체인) |
| 운영자 | 전체 | 서버를 설치하고 모델과 설정을 바꾸는 사람입니다. 인프라 전문가가 아닐 수 있습니다 | [설치 가이드](../guides/install/installation-guide.md) |
| 워크스페이스 | 전체 | 파일과 문서를 함께 쓰는 협업 단위입니다 | [Core](../architecture/blocks/core.md) |
| 원본 | 전체 | 사용자가 올린 파일입니다. 처리 결과와 따로 보관되며 결과가 없어도 온전합니다 | [Core](../architecture/blocks/core.md) |
| 임베딩 | 전체 | 문서 조각이나 질문을 벡터로 바꾸는 일입니다. 문서와 질문을 같은 모델로 바꿔야 비교할 수 있습니다 | [임베딩 런타임 가이드](../guides/operate/embedding-guide.md) |
| 작업 종류 | 기여자 | 역량이 수행하는 작업의 종류입니다. `parse_document`, `plan_document_embedding`, `embed_document_batch`, `embed_document`, `evaluate_retrieval`이 있고 각각 실행 역할과 큐가 정해져 있습니다 | [처리 파이프라인](../architecture/runtime/pipeline.md) |
| 청크 | 전체 | 검색에 쓰려고 문서를 나눈 조각입니다 | [처리 파이프라인](../architecture/runtime/pipeline.md) |
| 카탈로그 | 운영자, 기여자 | 선택할 수 있는 모델의 목록입니다. 모델 파일과 프로필 파일로 이루어집니다 | [카탈로그](../architecture/blocks/catalog.md) |
| 큐 | 운영자, 기여자 | 처리할 작업이 Redis에 쌓이는 대기열입니다. 파싱용 parse 큐와 임베딩용 embed 큐가 있습니다 | [처리 파이프라인](../architecture/runtime/pipeline.md) |
| 프로필 | 운영자, 기여자 | 카탈로그의 모델을 어떤 Provider, 차원, 저장소로 실행할지 적은 파일입니다 | [임베딩 설치 가이드](../guides/install/embedding-installation-guide.md) |
| 하이브리드 검색 | 전체 | dense와 sparse 벡터를 함께 써서 의미가 비슷한 문서와 단어가 일치하는 문서를 모두 찾는 검색입니다 | [검색과 RAG](../architecture/runtime/query.md) |
| 후속 작업 | 기여자 | job이 끝나면서 다음에 할 일로 선언하는 job입니다. executor의 처리 코드가 선언하고 Orchestrator가 검증해 발행합니다 | [처리 파이프라인](../architecture/runtime/pipeline.md#후속-작업의-선언과-발행) |

## 영문 용어

ABC 순입니다. 코드, 설정, 화면에 그대로 나오는 이름이라 영어로 씁니다.

| 용어 | 독자 | 뜻 | 자세한 문서 |
| --- | --- | --- | --- |
| Core | 기여자 | 계정, 워크스페이스, 파일을 합친 기반입니다. 문서 처리 기능 없이도 파일 관리가 동작합니다 | [Core](../architecture/blocks/core.md) |
| executor | 운영자, 기여자 | Orchestrator가 전달한 작업을 실제로 수행하는 프로세스입니다. 큐를 직접 소비하지 않습니다. parser-executor와 embedding-executor가 있습니다 | [배포](../architecture/deployment.md) |
| Function 계약 | 기여자 | 실행 역할이 제공하는 호출의 입출력 스키마입니다. `document.extract`, `text.embed`, `text.generate`가 있습니다. 역량 이름 `documents.extract`와 한 글자 차이지만 다른 선언입니다 | [Provider](../architecture/crosscutting/provider.md) |
| job | 기여자 | 작업 종류를 실제로 실행하는 한 건입니다. 식별자는 부모 job, 작업 종류, 대상에서 정해져서 같은 후속이 다시 발행되어도 같은 job이 됩니다 | [처리 파이프라인](../architecture/runtime/pipeline.md#후속-작업의-선언과-발행) |
| MCP 어댑터 | 통합 개발자 | AI 에이전트가 Dotori를 읽기 전용으로 조회하게 해 주는 어댑터입니다. CLI API를 호출합니다 | [인터페이스](./interfaces.md) |
| Orchestrator | 운영자, 기여자 | Redis 큐의 유일한 소비자입니다. 꺼낸 작업을 내부 HTTP로 executor에 전달하고 후속 작업을 발행합니다. 서비스 이름은 dotori-orchestrator입니다 | [오케스트레이션](../architecture/blocks/orchestration.md) |
| Provider | 운영자, 기여자 | 실행 역할이 어떤 구현을 쓸지 가리키는 이름입니다. 예를 들어 `bgem3_hybrid`입니다. 환경 변수와 일부 코드에서는 `backend`, 운영 화면에서는 "프로바이더"라고 부릅니다 | [Provider](../architecture/crosscutting/provider.md) |
| RAG | 전체 | 검색한 문서 조각을 근거로 LLM이 답변을 만드는 방식입니다 | [검색과 RAG](../architecture/runtime/query.md) |
| receipt | 기여자 | job의 식별자, 상태(실행 중, 성공, 실패), 시도 횟수, 결과 요약을 남기는 DB 기록입니다. 문서 내용과 벡터는 담지 않습니다. 같은 job의 중복 전달은 하나의 기록으로 수렴합니다 | [오케스트레이션](../architecture/blocks/orchestration.md) |
| runtime 설정 | 운영자, 기여자 | 설치하거나 명시적으로 바꿀 때 확정되어 기록되는 실제 사용 값입니다. 프로세스를 다시 시작해야 반영됩니다 | [설정 변경 적용](../guides/operate/applying-changes.md) |
