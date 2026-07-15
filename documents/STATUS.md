# STATUS

기준일: 2026-07-14
브랜치: `dev` / `main` 동기화 상태

1. Install 파이프라인 점검 (초기 요청)
카탈로그 로더, 관련 테스트 전수 확인 → 정상 동작 확인.
model-config.py(HF 메타데이터 조회 개발 도구)를 catalog/models/에서 별도 위치로 이동.
카탈로그에 아티팩트가 빠져 있던 gemma-3-27b-it 모델에 QAT GGUF 아티팩트 추가.
2. 서버 상태 조회 기능 신설
python manage.py server_status (신규 management command): 연결 상태(DB), 기능 on/off(파일 I/O·임베딩·RAG), 상세(모델/파라미터)를 3단으로 보고.
install.py --status: 호스트 쪽 docker 상태 + 컨테이너 내부 상태를 병합해 출력.
start.bat에 "Show Server Status" 메뉴 추가.
3. llm_installation_helper → llm_installation 패키지 재배치
document_ai 앱 내부에 있던 패키지를 app/ 최상위(document_ai, files와 같은 레벨)로 이동 — 순수 설치 로직이라 Django 앱일 필요가 없었음.
router.py의 "읽기 전용" 부분(ServerRAGTarget, get_cached_server_rag_target 등)을 document_ai/services/rag_runtime_config.py로 분리 → 실제 RAG 서빙 코드(llm_endpoint_service.py)가 더 이상 llm_installation을 import하지 않도록 결합 제거.
4. RAG 런타임 컨테이너 라이프사이클 전면 개편 (가장 큰 작업)
출발점 문제: llama-rag/vllm-rag가 항상 docker-compose.yml에 정의돼 있어 두 컨테이너가 동시에 뜨거나 방치될 수 있었고, 전환 실패 시 롤백도 없었음.

여러 대안 검토 후 최종 채택: Compose를 완전히 배제하고, app/llm_installation/runtime_lifecycle.py의 RuntimeLifecycleManager가 Docker CLI를 직접 호출해 컨테이너 하나를 전담 관리:

Scope 분리: production(dotori-runtime 네트워크, dotori-rag-runtime 컨테이너) / development(dotori-dev-runtime, dotori-dev-rag-runtime)를 완전히 분리.
Ownership label: com.dotori.managed/component/scope/runtime/generation으로 소유권을 표시하고, 이름은 같아도 label이 다르면 건드리지 않음(충돌 시 자동 삭제 방지).
Generation 기반 config + atomic commit: 후보 설정을 data/config/runtime_scopes/<scope>/generations/<id>/에 먼저 쓰고, 헬스체크를 통과해야만 llm_runtime.json을 원자적으로 교체.
Rollback: 전환 실패 시 기존 컨테이너를 rename으로 보존해뒀다가 복구(테스트 중 "같은 이름이라 rollback이 불가능한" 실제 버그를 발견해 수정).
install.py에 --stop [--scope production|development] 추가, start.bat의 Stop 메뉴 교체, detect_llm_runtime.py의 죽은 Docker 재시작 코드 제거.
새 테스트 3종(test_runtime_spec, test_runtime_lifecycle, test_config_generations) + 기존 test_llm_runtime_cleanup 재작성 — 가짜 docker 러너로 rollback/label 충돌까지 검증, 총 83개 통과.
검증 안 된 부분: 실제 Docker 데몬이 없는 환경이라 네트워크 연결, 실제 헬스체크, daemon 재시작 후 재시작 정책 동작은 실제 환경에서 확인이 필요합니다.