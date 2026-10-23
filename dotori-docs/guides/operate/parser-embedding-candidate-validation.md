# Parser와 임베딩 후보의 연결 검증

> 이전: [임베딩 Provider 선택과 후보 검증](./embedding-provider-validation.md) · 다음: [Provider 추가](../../architecture/extending/add-provider.md)

Parser가 실제로 선택한 tokenizer로 문서를 청킹할 수 있는지, 생성한 청크가 선택한 임베딩 Provider에서 실행되는지를 두 단계로 확인합니다. 운영 runtime, deployment plan, 문서 DB, 벡터는 변경하지 않습니다.

## Tokenizer 설정과 로딩

모델 JSON에 tokenizer의 저장소와 revision을 선언합니다. tokenizer에 custom code가 필요하면 다음 설정을 추가합니다.

```json
{
  "tokenizer_id": "owner/model",
  "tokenizer_revision": "fixed-commit-id",
  "tokenizer_options": {"trust_remote_code": true}
}
```

`tokenizer_options`는 현재 `trust_remote_code`만 허용합니다. 이 값은 임베딩 프로필의 `provider_options.trust_remote_code`와 독립적이며, 모델 로딩 권한을 Parser tokenizer에 자동으로 전파하지 않습니다. 실제 저장소의 코드와 고정 revision을 확인한 뒤 선언합니다.

설정은 모델 → resolved catalog → runtime으로 전달되며 fingerprint에 포함됩니다. 빈 tokenizer 옵션은 기존 fingerprint와 호환됩니다. Parser는 `AutoTokenizer.from_pretrained`로 tokenizer 파일만 로딩하며 임베딩 가중치를 로딩하지 않습니다.

운영 경로는 기존 fallback 정책을 유지합니다. 로딩 결과에는 requested/loaded ID와 revision, 관찰한 commit, fallback 여부와 이유를 기록합니다. raw tokenizer와 Docling wrapper는 같은 로딩 결과를 공유합니다. Parser healthcheck의 `tokenizer_identity`에서 실제 선택 경로를 확인할 수 있습니다. 관찰한 commit이 없으면 요청 revision을 실제 관찰값으로 대신 쓰지 않습니다.

후보 경로는 명시적인 runtime을 받고 fallback을 허용하지 않습니다. 선택한 tokenizer가 로딩되지 않으면 Parser 검증은 실패합니다.

## 1. Parser 프로세스에서 청크 보고서 생성

공유 임시 디렉터리에 작은 샘플 Markdown 문서를 준비합니다. 다음 예시는 카탈로그의 MiniLM을 사용합니다. 카탈로그 밖 후보는 두 명령 모두 같은 `--model-file`과 `--profile-file` 쌍으로 대체합니다.

```bash
mkdir -p /tmp/dotori-parser-validation
# /tmp/dotori-parser-validation/sample.md 에 검증할 샘플 문서를 준비
docker compose run --rm --no-deps \
  -v "$PWD/app:/app:ro" \
  -v /tmp/dotori-parser-validation:/validation \
  parser-executor python manage.py validate_embedding_parser_candidate \
  --catalog-id all-minilm-l6-v2 \
  --document-file /validation/sample.md \
  --result-output /validation/parser-report.json
```

`--document-file`을 생략하면 DoclingDocument로 만든 고정 fixture를 청킹하며, 이 경우 파일 변환은 미검증으로 표시합니다. 파일을 지정하면 Docling converter로 변환하고 정상 완료를 요구합니다. 진단 명령은 Docling이 직접 지원하는 파일을 대상으로 하며 HWP의 기존 전처리 facade까지 시험하지는 않습니다.

후보 tokenizer를 Docling HuggingFaceTokenizer로 감싸 HybridChunker에 전달합니다. 기존 CustomSerializerProvider와 동일한 정규화와 contextualize 처리를 사용합니다. 청크의 텍스트와 tokenizer별 토큰 수를 보고서에 기록합니다.

`--token-headroom`의 후보 검증 기본값은 128이며 chunk budget은 runtime max_tokens에서 이를 뺀 값입니다. 운영 서버와 동일한 조건을 시험하려면 서버의 headroom을 명시합니다. 음수이거나 0인 budget은 거부합니다. `--max-chunks`의 기본값은 64이며, 이를 초과하면 일부만 검증하고 성공 처리하지 않고 실패합니다.

## 2. 모델 프로세스에서 청크 임베딩 검증

```bash
docker compose run --rm --no-deps \
  -e DOTORI_EMBEDDING_MODEL_PROCESS=1 \
  -v "$PWD/app:/app:ro" \
  -v /tmp/dotori-parser-validation:/validation \
  embedding-executor python manage.py validate_embedding_candidate \
  --catalog-id all-minilm-l6-v2 \
  --parser-report /validation/parser-report.json \
  --result-output /validation/embedding-report.json
```

먼저 Parser 보고서의 성공 상태, 내용 digest, 후보 설정, fallback 여부를 확인합니다. 날짜나 진단 generation처럼 실행마다 달라지는 값은 비교에서 제외하지만, 모델·tokenizer revision, 옵션, Provider, 차원, 입력 제한 등은 일치해야 합니다. digest는 파일의 변경을 탐지하는 값이며 서명된 function evidence가 아닙니다.

로컬 모델은 실제 모델 tokenizer와 기본 입력 제한을 사용합니다. document prefix와 명시적·기본 named prompt까지 포함한 최종 길이를 계산합니다. 초과한 입력은 뒤쪽 내용을 줄이고 실제 길이를 다시 검사하며, 필수 prompt만으로 budget이 소진되면 실패합니다. ST와 BGE Provider의 실제 실행 경로에도 이 처리를 적용합니다. BGE-M3의 cap은 최대 8192입니다.

모든 청크를 작은 배치로 실행하고 개수, 차원, 값, 정규화, sparse 계약을 확인합니다. `parser_pipeline` 결과에 chunk_count, truncated_chunks, effective_token_cap과 Parser 식별 정보를 표시합니다. 외부 API는 선언한 Parser tokenizer로 길이를 맞추지만, 외부 서비스의 실제 tokenizer와 cap이 일치하는지는 별도 미검증으로 남깁니다.

## 완료의 의미

두 보고서의 `passed`와 `remaining_checks`를 함께 확인합니다. Tokenizer, Docling 청킹, Provider 연결을 통과했더라도 운영 HTTP routing, DB schema, HWP 전처리, OCR의 모든 파일 형식, 검색 품질, 메모리 부하, 배포·복원 검증을 대신하지는 않습니다. 보고서에는 샘플 문서의 청크 본문이 들어 있으므로 진단 파일로 따로 관리합니다.

이 검증은 새 상주 서비스, 큐, DB 모델, 임베딩 모델 선택 환경변수를 추가하지 않습니다. 기존 모델 프로세스 플래그는 일회성 embedding executor의 소유권을 표시합니다. Parser와 embedding 프로세스는 종료되면 후보 캐시와 메모리를 공유하지 않습니다. 운영 모델 교체는 기존 변경·재임베딩 절차를 따릅니다.
