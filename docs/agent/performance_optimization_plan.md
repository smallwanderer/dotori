# Dotori Performance Optimization Plan

## 목적

이 문서는 Dotori의 응답시간을 줄이고 처리량을 높이기 위한 성능 개선 계약과 실행 순서를 정의한다.

Dotori는 운영자 한 명이 서버 단위로 모델과 런타임을 관리하는 셀프호스팅 서비스다. 성능 개선은 다음 원칙을 따른다.

- Django 요청 경로에서는 무거운 AI 작업을 실행하지 않는다.
- 모델과 런타임은 설치 또는 운영자가 명시적으로 변경할 때만 결정한다.
- 일반 RAG 요청은 `data/config/llm_runtime.json`에 저장된 설정을 사용한다.
- 처리량을 높이기 전에 메모리, DB connection, queue, LLM slot의 안전 한도를 확인한다.
- 캐시와 병렬성은 사용자 데이터 격리와 결과 정확성을 훼손하지 않아야 한다.
- 변경 전후의 p50, p95, p99와 처리량을 같은 부하 조건에서 비교한다.

## 현재 구조

```text
Client
  -> Nginx
  -> Django
       -> Redis / Celery
            -> parse, embed
            -> search
            -> query
            -> rag
                 -> llama.cpp or vLLM
       -> PostgreSQL + pgvector
```

현재 검색과 RAG API는 작업을 큐에 등록하고 `202 Accepted`와 polling URL을 반환한다. worker는 `embedding-worker`, `search-worker`, `query-worker`, `rag-worker`로 분리되어 있지만 기본 concurrency는 모두 1이다. `embedding-worker`는 `parse`와 `embed` queue를 함께 소비한다.

## 우선순위

| 우선순위 | 문제 | 목표 |
| --- | --- | --- |
| P0 | 성능 관측성 부족 | 병목과 개선 효과를 수치로 판단 |
| P0 | HNSW opclass와 검색 거리 전략 불일치 | 벡터 인덱스가 실제 검색에 사용되도록 수정 |
| P0 | LLM runtime 병렬 용량과 RAG 동시성 불일치 | 안전하게 계산된 LLM 처리량을 실제 요청에 적용 |
| P0 | end-to-end 토큰 스트리밍 부재 | 첫 토큰 체감시간 단축 |
| P1 | evidence context N+1 조회 | 검색 후처리 DB 왕복 감소 |
| P1 | parse와 embed의 worker 공유 | 자원 경합 제거 및 독립 확장 |
| P1 | embedding micro-batching 부재 | GPU/CPU 임베딩 처리량 향상 |
| P1 | PostgreSQL connection 재사용·pool 부재 | worker 확장 시 연결 비용과 고갈 방지 |
| P2 | 반복 계산 캐시 부재 | query embedding과 반복 검색 비용 감소 |
| P2 | polling 중심 상태 전달 | DB 읽기 부하와 완료 인지 지연 감소 |
| P3 | 정적 자산 전달 최적화 부족 | 원격 접속 초기 화면 로딩 개선 |

## P0-1. 성능 관측성

### 구현 상태

초기 관측 기반이 구현되어 있다.

- `SearchJob.performance_metrics`와 `RAGJob.performance_metrics`에 단계별 millisecond 값을 저장한다.
- 검색은 queue wait, query embedding, embedding presence check, vector query, rerank/evidence, contextual compression, worker total, end-to-end 시간을 기록한다.
- RAG는 queue wait, generation queue wait, context build, semaphore wait, LLM connect, TTFT, first-token 이후 생성, LLM total, worker total, end-to-end 시간을 기록한다.
- job API serializer와 Django admin에서 저장된 metrics를 조회할 수 있다.
- PostgreSQL은 `pg_stat_statements`를 preload하고 extension migration을 적용한다.
- 이 단계의 JSON metrics는 durable baseline이다. Prometheus exporter와 집계 dashboard는 후속 작업으로 남아 있다.

### 필요성

현재 작업의 성공·실패와 시작·완료 시각은 일부 저장되지만 queue 대기, query embedding, 벡터 SQL, compression, LLM 첫 토큰 시간을 통합해서 볼 수 없다. 실측 없이 concurrency, cache, pool을 변경하면 병목을 다른 단계로 이동시키거나 안정성을 낮출 수 있다.

### 계획

다음 구간을 독립적으로 계측한다.

```text
request accepted
  -> queue wait
  -> query embedding
  -> vector SQL
  -> sparse reranking
  -> contextual compression
  -> RAG queue wait
  -> semaphore wait
  -> LLM first token
  -> generation complete
```

최소 지표는 다음과 같다.

- `search.queue_wait_ms`
- `search.embedding_ms`
- `search.vector_query_ms`
- `search.compression_ms`
- `rag.queue_wait_ms`
- `rag.semaphore_wait_ms`
- `rag.ttft_ms`
- `rag.generation_ms`
- `rag.output_tokens`
- queue depth, DB connection 수, cache hit ratio

PostgreSQL에는 `pg_stat_statements`를 활성화하고 대표 검색 SQL을 `EXPLAIN (ANALYZE, BUFFERS)`로 검증한다. 부하 테스트는 청크 수와 동시 요청 수를 단계적으로 늘려 반복 가능하게 만든다.

### 완료 기준

- 검색과 RAG p50, p95, p99를 확인할 수 있다.
- queue부터 LLM까지 처리시간을 분해할 수 있다.
- 상위 slow query와 인덱스 사용 여부를 확인할 수 있다.
- 같은 데이터와 부하로 변경 전후 결과를 비교할 수 있다.

### 기대효과

- 효과가 없는 최적화를 피한다.
- 하드웨어별 concurrency와 batch 크기를 근거 있게 결정한다.
- 성능 회귀를 배포 전에 발견한다.

## P0-2. HNSW와 거리 전략 정합성

### 현재 문제

`ChunkEmbedding.vector`의 HNSW 인덱스는 `vector_cosine_ops`를 사용하지만 기본 검색 설정은 `inner_product`이고 검색 코드는 `MaxInnerProduct`를 사용한다. 연산자 클래스가 다르면 PostgreSQL이 해당 HNSW 인덱스를 검색 정렬에 사용할 수 없다.

### 계획

1. BGE-M3 벡터 정규화와 검색 품질을 확인하고 공식 거리 정책을 하나로 확정한다.
2. `inner_product`를 유지하면 HNSW를 `vector_ip_ops`로 변경한다.
3. 운영 데이터에서는 새 인덱스를 concurrent 방식으로 먼저 생성한다.
4. 실행계획에서 HNSW Index Scan을 확인한다.
5. `m`, `ef_construction`, `hnsw.ef_search`를 golden set과 latency를 함께 보며 조정한다.
6. 구 인덱스는 검증 완료 후 제거한다.

### 완료 기준

- 설정, ORM distance 연산, HNSW opclass가 일치한다.
- 실행계획에서 HNSW 인덱스를 사용한다.
- 중·대형 데이터셋에서 p95가 개선된다.
- 기존 검색 품질이 허용 범위 안에서 유지된다.
- migration과 rollback 절차가 있다.

### 기대효과

- 청크 증가에 따른 검색시간 증가를 억제한다.
- PostgreSQL CPU와 search-worker 대기시간을 줄인다.
- 검색을 선행하는 RAG의 응답시간도 함께 줄어든다.

## P0-3. LLM 병렬 용량과 실제 동시성 연동

### 현재 문제

설치 서비스는 메모리 안전 범위에서 llama.cpp `parallel` 또는 vLLM `max_num_seqs`를 계산한다. 그러나 RAG 경로의 `RAG_SEMAPHORE_COUNT`와 `RAG_WORKER_CONCURRENCY` 기본값은 1이다. 런타임이 여러 slot을 지원해도 실제 요청은 직렬 처리될 수 있다.

반대로 worker와 semaphore만 높이면 runtime slot을 초과해 queueing, OOM, latency 급증이 발생할 수 있다.

### 계획

`llm_runtime.json`의 serving profile을 동시성의 기준으로 사용한다.

```text
llama.cpp: RAG_SEMAPHORE_COUNT <= --parallel
vLLM:      RAG_SEMAPHORE_COUNT <= max_num_seqs
RAG_WORKER_CONCURRENCY >= RAG_SEMAPHORE_COUNT
```

런타임 변경은 다음을 하나의 전환 절차로 처리한다.

1. 새 config 기록
2. 선택 runtime 시작
3. health check
4. 비선택 runtime 중지
5. semaphore와 worker concurrency 반영
6. `rag-worker` 재시작
7. smoke test와 동시 부하 검증

동시 요청 한도를 초과하면 queue depth와 대기시간을 제한하고 취소된 작업은 semaphore를 즉시 반환해야 한다.

### 완료 기준

- persisted runtime profile과 실제 runtime·semaphore·worker 설정이 일치한다.
- runtime slot을 넘는 요청이 모델로 전달되지 않는다.
- 동시 부하에서 OOM이 발생하지 않는다.
- 모델 변경 후 새 동시성이 자동 반영된다.
- 1, 2, 4 동시 요청의 p95와 aggregate tokens/sec가 기록된다.

### 기대효과

- 하드웨어가 허용하는 LLM 처리량을 실제로 사용한다.
- 불필요한 직렬 처리와 queue 대기를 줄인다.
- 과도한 동시 실행으로 인한 런타임 장애를 방지한다.

## P0-4. End-to-end 토큰 스트리밍

### 현재 문제

rag-worker는 LLM의 streaming 응답을 읽지만 모든 delta를 메모리에 모은 후 최종 답변만 DB에 저장한다. 브라우저는 polling으로 완료된 답변을 확인하므로 사용자가 느끼는 첫 응답시간은 전체 생성시간과 같다.

### 계획

단방향 토큰 전달에는 Redis Streams와 SSE를 우선 사용한다.

```text
LLM
  -> rag-worker
  -> Redis Stream
  -> Django SSE endpoint
  -> Browser
```

이벤트 계약은 최소한 다음을 포함한다.

```json
{"type":"stage","stage":"generating"}
{"type":"delta","seq":1,"text":"..."}
{"type":"citation","items":[]}
{"type":"done","job_id":123}
{"type":"error","message":"..."}
```

- Redis는 실시간 delta를 짧게 보존한다.
- PostgreSQL은 최종 답변과 영속 상태의 기준으로 유지한다.
- 매 토큰마다 DB에 쓰지 않는다.
- `Last-Event-ID` 또는 `seq`를 이용해 재접속을 지원한다.
- Redis/SSE 장애 시 기존 polling으로 fallback한다.
- Nginx의 SSE 경로는 `proxy_buffering off`를 사용한다.
- 명시적 취소는 LLM 연결 종료와 semaphore 반환까지 수행한다.

### 완료 기준

- 생성 완료 전 첫 토큰이 브라우저에 표시된다.
- 재접속과 명시적 취소가 동작한다.
- Redis 장애 시 polling으로 최종 결과를 받을 수 있다.
- DB에 토큰별 write가 발생하지 않는다.
- 운영 Nginx를 통과한 환경에서 검증한다.

### 기대효과

- 총 생성시간이 같아도 체감 응답시간이 크게 개선된다.
- 긴 답변의 진행 상태가 명확해진다.
- 잘못된 답변을 조기에 취소하여 자원을 절약한다.

## P1-1. Evidence context N+1 제거

### 현재 문제

최종 evidence마다 인접 청크를 별도 쿼리로 조회한다. evidence 수가 늘어나면 SQL 수도 함께 증가한다.

### 계획

1. 최종 evidence 목록을 먼저 확정한다.
2. 필요한 `(parse_result_id, chunk_index range)`를 계산한다.
3. 관련 청크를 한 번 또는 소수 쿼리로 일괄 조회한다.
4. `(parse_result_id, chunk_index)` map을 구성한다.
5. 메모리에서 evidence별 context를 조립한다.
6. 결과 수가 증가해도 쿼리 수가 일정한 query-count 테스트를 추가한다.

### 완료 기준

- evidence 수와 무관하게 인접 청크 쿼리 수가 일정하다.
- 기존 context 결과와 동일하다.
- 다른 문서의 청크가 섞이지 않는다.
- 검색 후처리 p95가 개선된다.

### 기대효과

- DB 왕복과 connection 점유시간을 줄인다.
- HNSW 개선 효과가 후처리 쿼리에 의해 상쇄되지 않는다.

## P1-2. Parse와 Embed worker 분리

### 현재 문제

하나의 `embedding-worker`가 `parse,embed` queue를 함께 소비한다. CPU·RAM 중심 파싱과 GPU/CPU 모델 중심 임베딩이 같은 worker pool과 concurrency를 공유한다.

### 계획

```text
parse-worker: -Q parse
embed-worker: -Q embed
```

- 초기에는 같은 AI 이미지를 재사용해 변경 범위를 줄인다.
- worker별 concurrency와 CPU·RAM·GPU 제한을 분리한다.
- embed queue depth를 모니터링하고 parser가 무제한 backlog를 만들지 않도록 backpressure를 둔다.
- retry와 recovery가 새 queue 구조에서도 동작하는지 통합 테스트한다.

### 완료 기준

- 별도 컨테이너와 독립 concurrency로 실행한다.
- 대형 문서 파싱 중에도 임베딩 작업이 진행된다.
- 한 worker의 재시작이 다른 단계에 영향을 주지 않는다.
- recovery와 재시도가 정상 작동한다.

### 기대효과

- 파싱과 임베딩의 자원 경합을 줄인다.
- 각 단계를 독립적으로 확장하고 장애를 격리한다.

## P1-3. Embedding micro-batching

### 현재 문제

현재 청크마다 Celery task와 단건 embedding 호출이 발생한다. 짧은 청크가 많으면 broker, DB 상태 변경, 모델 호출, 작은 GPU kernel의 오버헤드가 커진다.

### 계획

provider contract에 batch API를 추가한다.

```python
embed_documents(texts: list[str]) -> list[EmbeddingResult]
```

- 초기에는 문서별 batch task를 사용한다.
- batch는 청크 수뿐 아니라 총 토큰 예산으로 제한한다.
- 입력과 출력의 개수·순서를 검증한다.
- OOM이면 batch 크기를 절반으로 줄여 재시도한다.
- 실패 청크만 재시도하고 최종적으로 단건 fallback을 허용한다.
- 실제 처리량을 확인한 뒤 cross-document dynamic batching을 검토한다.

### 완료 기준

- batch embedding이 벡터 정확성을 유지한다.
- 부분 실패와 OOM 축소 재시도가 동작한다.
- 단건 대비 chunks/sec가 개선된다.
- queue 적체와 문서 준비 완료시간이 감소한다.

### 기대효과

- GPU 활용률을 높이고 task·DB 오버헤드를 줄인다.
- 같은 하드웨어에서 더 많은 문서를 처리한다.

## P1-4. Connection Pooling

### 현재 문제

Django DB 설정에 persistent connection, Django pool 또는 PgBouncer가 없다. worker concurrency와 replica를 늘리면 연결 생성 비용, PostgreSQL `max_connections`, connection storm이 병목이 될 수 있다.

### 계획

소규모 단일 서버에서는 먼저 다음을 적용한다.

```text
DB_CONN_MAX_AGE=60
DB_CONN_HEALTH_CHECKS=1
```

다음으로 app, worker, beat, admin, maintenance connection을 합산해 연결 예산을 계산한다. worker가 많거나 재시작이 잦으면 PgBouncer transaction pooling을 추가한다.

PgBouncer 도입 시 migration, Celery result backend, 장시간 transaction, server-side cursor, 장애 복구를 검증한다.

### 완료 기준

- 부하 중 DB connection 수가 예산 안에서 유지된다.
- worker 재시작 시 connection storm이 없다.
- 끊어진 연결이 복구된다.
- migration과 Celery 결과 저장이 정상이다.
- pool wait time을 관찰할 수 있다.

### 기대효과

- worker concurrency를 안전하게 높일 수 있다.
- 연결 생성시간과 PostgreSQL 메모리 사용을 안정화한다.

## P2-1. Redis 캐싱

### 대상

우선 적용 대상은 query embedding이다. 다음으로 짧은 TTL의 검색 결과, folder tree, AI readiness, 완료된 job 상태를 검토한다. RAG 답변은 prompt·model·권한·최신성 조건이 복잡하므로 기본 캐시 대상에서 제외한다.

### 키 설계

Query embedding key는 다음을 포함한다.

```text
backend + model + model_version + normalization_version + normalized_query_hash
```

검색 결과 key는 다음을 포함한다.

```text
user_id + normalized_query + scope + top_k + threshold
+ tuning params + embedding version + user search_index_version
```

사용자 ID를 반드시 포함한다. 업로드, 삭제, 복원, 파싱, 임베딩, AI 처리 상태 변경 시 사용자별 `search_index_version`을 증가시켜 stale cache를 무효화한다.

### 완료 기준

- 사용자 간 캐시 격리 테스트가 있다.
- 데이터 변경 후 stale 결과가 반환되지 않는다.
- Redis 장애 시 정상 계산으로 fallback한다.
- hit, miss, eviction, latency를 측정한다.

### 기대효과

- 반복 query embedding과 검색 비용을 줄인다.
- search-worker, DB, CPU/GPU 부하를 낮춘다.

## P2-2. Polling 개선

### 계획

- 초기 polling은 짧게, 장기 작업은 지수 backoff를 사용한다.
- 서버는 `next_poll_ms` 또는 `Retry-After`를 제공한다.
- 완료된 상태는 짧은 Redis 캐시를 사용할 수 있다.
- SSE가 도입되면 stage 이벤트를 동일 채널로 전달한다.
- SSE 실패 시 polling을 fallback으로 유지한다.

### 완료 기준

- 작업당 평균 polling 횟수가 감소한다.
- 완료 인지 지연은 허용 범위 안에 있다.
- DB를 영속 상태의 기준으로 유지한다.

### 기대효과

- app과 DB의 반복 읽기를 줄인다.
- 상태 변경을 더 빠르게 사용자에게 전달한다.

## P3. 정적 자산과 CDN

CDN은 검색과 RAG 생성시간을 줄이지 않으므로 후순위다. 우선 Nginx와 Django 정적 파일 정책을 정리한다.

- content hash 기반 파일명
- `/static/`에 `Cache-Control: public, max-age=31536000, immutable`
- HTML은 `no-cache`
- 인증 문서는 `private, no-store`
- gzip 또는 Brotli

외부 CDN이 필요하면 `/static/`부터 적용한다. `/media/`, `/api/`, `/accounts/`, `/files/`, RAG 응답은 기본적으로 CDN에서 제외한다.

## 실행 로드맵

### Phase 1: 측정과 검색 기반

1. 성능 지표와 부하 테스트 구축
2. `pg_stat_statements` 및 실행계획 수집
3. HNSW opclass 정합성 수정
4. evidence N+1 제거

### Phase 2: LLM 처리량과 체감 응답

1. runtime parallel과 semaphore 연동
2. rag-worker concurrency 연동
3. 동시 부하 및 OOM 검증
4. Redis Streams와 SSE 기반 토큰 스트리밍
5. 취소, 재접속, polling fallback

### Phase 3: 문서 처리량

1. parse/embed worker 분리
2. provider batch API
3. embedding micro-batching
4. queue backpressure와 recovery 검증

### Phase 4: DB와 반복 요청

1. persistent DB connection
2. 필요 시 PgBouncer
3. query embedding 캐시
4. 검색·메타데이터 캐시
5. polling 감소

### Phase 5: 전달 계층

1. immutable static assets
2. 압축
3. 필요 시 `/static/` CDN

## 변경 금지 사항

- 일반 요청에서 하드웨어 탐지나 모델 재선택을 실행하지 않는다.
- 사용자 입력으로 임의의 worker/runtime concurrency를 무제한 적용하지 않는다.
- 사용자 ID와 index version이 없는 검색 결과 캐시를 만들지 않는다.
- 토큰 delta마다 PostgreSQL에 write하지 않는다.
- HNSW opclass를 변경하고 실행계획과 검색 품질을 검증하지 않은 채 완료 처리하지 않는다.
- `/media/` 또는 인증 문서를 public CDN cache에 저장하지 않는다.
- 측정 결과 없이 복합 인덱스를 대량 추가하지 않는다.

## 최종 목표 구조

```text
Client
  -> CDN (/static only, optional)
  -> Nginx
  -> Django / SSE
       -> Redis cache and streams
       -> PgBouncer (when required)
       -> PostgreSQL + pgvector/HNSW
       -> Celery
            -> parse-worker
            -> embed-worker with batching
            -> search-worker
            -> query-worker
            -> rag-worker
                 -> selected llama.cpp or vLLM runtime
```

성능 개선의 완료는 기능 구현 여부가 아니라 동일한 데이터와 부하에서 정확성을 유지하면서 p95, queue wait, DB 부하, 처리량이 개선되었는지로 판단한다.
