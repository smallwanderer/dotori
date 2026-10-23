# 임베딩 Provider 선택과 후보 검증

> 이전: [Provider](../../architecture/crosscutting/provider.md) · 다음: [Parser와 임베딩 후보의 연결 검증](./parser-embedding-candidate-validation.md)

문서 → 청크 → 임베딩 연결은 [Parser 후보 연결 검증](parser-embedding-candidate-validation.md)의 두 단계 명령을 사용합니다. `--parser-report`를 생략한 기본 명령은 해당 연결을 미검증으로 표시합니다.

새 모델마다 Provider를 구현할 필요는 없습니다. 모델의 공식 실행 방법과 필요한 출력에 맞춰 기존 Provider를 선택하고, 운영 모델을 바꾸기 전에 후보 프로필을 시험합니다.

## Provider 선택

| Provider ID | 선택 조건 | 대표 사례 |
| --- | --- | --- |
| `sentence_transformers` | SentenceTransformer로 문장당 dense 벡터 생성 가능 | MiniLM, E5, BGE v1.5, BGE-M3 dense, Qwen3 Embedding, Jina v3 |
| `bgem3_hybrid` | BGEM3FlagModel로 BGE-M3 dense와 native sparse를 함께 사용 | BGE-M3 hybrid |
| `openai_compatible` | OpenAI embeddings 요청·응답 계약을 제공하는 endpoint | OpenAI 호환 로컬 서버 또는 외부 서비스 |

BGE-M3를 SentenceTransformers로 실행하면 dense는 생성할 수 있지만, 현재 해당 Provider는 native sparse를 제공하지 않습니다. 이 경우 `supports_sparse: false`로 작성합니다. 기존 `bgem3_hybrid`는 dense와 sparse를 반환하며 ColBERT 출력은 사용하지 않습니다. [BGE-M3 공식 문서](https://huggingface.co/BAAI/bge-m3)

라이브러리 지원과 Dotori의 실행 검증은 다릅니다. 모델, revision, 옵션, 라이브러리, 하드웨어 조합을 실제로 시험해야 합니다. 전용 API 계약이 다른 서비스는 전용 어댑터나 호환 gateway가 필요합니다. Multi-vector 저장과 검색은 현재 계약의 범위 밖입니다.

## 프로필 옵션

모델 JSON은 기본 차원과 revision을, 프로필 JSON은 Provider와 실제 저장 차원·처리를 지정합니다. 기존 프로필은 `provider_options`를 생략해도 됩니다. 알 수 없는 옵션이나 다른 Provider의 옵션은 거부합니다.

SentenceTransformers의 허용 옵션은 다음과 같습니다.

| 옵션 | 의미 |
| --- | --- |
| `trust_remote_code` | 기본값은 false입니다. 모델 저장소의 custom code 실행을 허용합니다. 사용할 때는 해당 코드를 확인하고 revision을 고정합니다 |
| `truncate_dim` | 출력 차원을 줄입니다. 프로필 dimension과 같아야 하고 모델 기본 차원 이하여야 합니다 |
| `query_task`, `document_task` | encode에 전달할 모델별 task입니다 |
| `query_prompt_name`, `document_prompt_name` | 모델에 저장된 prompt 이름입니다. 같은 입력의 prefix와 동시에 지정할 수 없습니다 |

예를 들어 Jina v3의 공식 사용법은 custom code와 retrieval task, prompt를 사용합니다. 다음은 프로필의 옵션 부분이며, 모델 JSON의 revision과 나머지 프로필 필드도 작성해야 합니다. [Jina 공식 사용법](https://huggingface.co/jinaai/jina-embeddings-v3)

```json
{
  "provider_options": {
    "trust_remote_code": true,
    "query_task": "retrieval.query",
    "query_prompt_name": "retrieval.query",
    "document_task": "retrieval.passage",
    "document_prompt_name": "retrieval.passage"
  }
}
```

Qwen3 4B/8B의 기본 차원은 2560/4096입니다. 현재 저장소는 384, 640, 768, 1024, 1536만 지원하므로, 모델이 지원하는 차원 축소를 명시해야 합니다. 아래는 1024차원 프로필의 일부입니다. 단순히 dimension만 바꾸면 출력이 축소되지 않습니다. [Qwen3 모델 정보](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)

```json
{
  "provider": "sentence_transformers",
  "dimension": 1024,
  "store": "pgvector_chunk_1024",
  "provider_options": {"truncate_dim": 1024}
}
```

차원 축소 옵션이 모델의 검색 품질이나 모델 고유 후처리까지 보장하지는 않습니다. Nomic처럼 공식 사용법에 추가 후처리가 있는 모델은 축소 경로를 별도로 확인합니다. [Nomic 공식 사용법](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)

OpenAI 호환 Provider는 `send_dimensions`를 지원합니다. true이면 모델 이름과 관계없이 프로필 dimension을 API의 `dimensions`로 전달하고, false이면 생략합니다. 옵션을 지정하지 않으면 기존 동작을 유지합니다(text-embedding-3 이름에서만 전달). endpoint가 이 옵션을 지원하는지 실행해서 검증합니다. URL과 키는 기존 서버 단위 endpoint 설정을 사용하며 프로필에 비밀키를 저장하지 않습니다.

옵션을 바꾸면 runtime fingerprint가 바뀝니다. 빈 옵션은 기존 runtime fingerprint와 호환됩니다. 검증에 성공해도 운영 설정이 적용되거나 기존 벡터가 새 모델과 호환되는 것은 아닙니다.

## 후보 검증 명령

이 명령은 운영 runtime, deployment plan, generation row, 저장된 벡터를 변경하지 않습니다. 모델 다운로드 캐시와 명시적으로 지정한 새 보고서 파일은 생성될 수 있습니다. `--result-output`은 기존 파일을 덮어쓰지 않습니다.

카탈로그 항목의 설정만 검증하는 명령입니다.

```bash
docker compose run --rm --no-deps \
  -v "$PWD/app:/app:ro" \
  embedding-executor python manage.py validate_embedding_candidate \
  --catalog-id bge-m3-hybrid --config-only
```

카탈로그 밖의 모델과 프로필 JSON을 시험하려면 두 파일을 `/tmp/dotori-embedding-validation/model.json`, `profile.json`에 준비합니다. 이때 프로필의 `model_id`는 모델의 `id`와 같아야 합니다. 설정만 검사하려면 `--config-only`를 추가합니다.

```bash
mkdir -p /tmp/dotori-embedding-validation
docker compose run --rm --no-deps \
  -e DOTORI_EMBEDDING_MODEL_PROCESS=1 \
  -v "$PWD/app:/app:ro" \
  -v /tmp/dotori-embedding-validation:/validation \
  embedding-executor python manage.py validate_embedding_candidate \
  --model-file /validation/model.json \
  --profile-file /validation/profile.json \
  --result-output /validation/report.json
```

실제 실행 검증은 위처럼 일회성 embedding executor 컨테이너에서 수행합니다. 이 컨테이너는 기존 HTTP 프로세스의 모델 캐시를 공유하지 않으며, 종료되면 후보 모델 메모리가 해제됩니다. 실행 중에는 후보 모델의 메모리가 추가로 필요하므로, 여유가 없으면 유지보수 시간에 운영 작업을 중지하고 시험합니다. 후보 명령이 운영 executor를 자동으로 중지하거나 GPU 메모리를 예약하지는 않습니다. 새 상주 서비스는 없습니다.

로컬 캐시만 사용하려면 기존 `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`을 컨테이너에 지정합니다. 라이브러리가 없거나 모델 파일, custom code, task가 호환되지 않으면 실패 항목에 기록됩니다.

## 결과 해석

설정 오류나 실행 계약 실패가 있으면 JSON 보고서를 출력하고 종료 코드는 0이 아닙니다. 모델이나 프로필 파일의 구문 오류도 보고서에 기록합니다.

| 검사 | 확인 내용 |
| --- | --- |
| `configuration` | 알려진 Provider, 차원과 store의 짝, native sparse 선언, 현재 검색의 normalized inner_product 조건 |
| `model_process` | 구현 생성이 모델 소유 프로세스에서 수행되는지 |
| `single_outputs` | query/document 호출, 출력 차원, 메타데이터, 유효값, 정규화, sparse 선언 |
| `batch_outputs` | 출력 개수, 각 입력의 단일 호출과 비교한 순서와 dense 일관성 |
| `token_limit` | 로컬 모델에서 oversized 입력과 정확히 token-truncated된 입력의 결과 일치 |

`passed: true`는 실행한 검사들이 통과했다는 뜻입니다. `mode: config_only`는 모델 실행 검증이 아니므로 `remaining_checks`를 함께 읽습니다. 외부 API의 실제 revision과 token cap, 운영 DB schema, HTTP routing, 검색 품질은 이 명령이 보장하지 않습니다. 실제 관찰한 model/tokenizer revision과 요청한 revision도 구분하며, 관찰할 수 없는 artifact identity는 미검증으로 남깁니다.

토큰 fixture가 모델의 prompt/tokenizer와 일치하지 않으면 token cap 통과를 발급하지 않고 실패 이유를 기록합니다. 보고서는 카탈로그의 function evidence를 생성하거나 자동 승격하지 않습니다. 성공한 뒤에는 [명시적 모델 변경 절차](embedding-guide.md)를 진행하고, 재임베딩과 운영 요청, 검색 품질을 별도로 확인합니다.
