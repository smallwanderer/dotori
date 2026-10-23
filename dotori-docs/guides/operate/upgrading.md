# 기존 설치 업데이트

> 이전: [백업과 데이터 보관](./backup-and-storage.md) · 다음: [설정 변경 적용](./applying-changes.md)

코드 업데이트는 이미지를 만드는 단계와 실행 중인 서비스를 교체하는 단계로 나눕니다. 아래는 Documents 검색·RAG 구성의 App·Orchestrator·Parser·Embedding을 갱신하는 절차입니다. 기본·추출 구성에서는 설치하지 않은 executor를 추가로 시작하지 않습니다.

## 업데이트 전 확인

현재 버전과 새 버전의 릴리스 노트를 확인하고 이전 코드·이미지를 보존합니다. `v0.4.0`은 문서 앱 이름과 migration 이력 변경이 있으므로 새 버전의 소스를 받은 뒤 기존 서비스를 개별적으로 재시작하지 않습니다.

```bash
docker compose ps
python install.py --status
```

Windows에서는 C: 등 Docker 저장소가 위치한 드라이브의 실제 여유 공간을 확인합니다. 캐시가 없으면 AI 패키지를 다시 내려받아야 하므로 이미지 최종 크기보다 많은 임시 공간이 필요합니다.

## 1. 새 이미지 빌드

선택한 릴리스 소스를 받은 뒤 사용 중인 서비스만 하나씩 빌드합니다. 빌드는 기존 컨테이너를 교체하지 않습니다.

```bash
docker compose build app
docker compose build dotori-orchestrator
docker compose build parser-executor
docker compose build embedding-executor
```

실패하면 여기서 원인을 확인합니다. 모든 빌드가 성공하기 전에는 서비스 교체를 시작하지 않습니다. 일반 코드 변경에 `--no-cache`를 붙일 필요는 없습니다.

## 2. 접수 중지와 작업 종료

사용자에게 점검을 알리고 새 업로드·검색·질문을 중지합니다. 진행 중인 RAG가 끝난 뒤 App을 중지하고, 기존 Orchestrator에서 문서 작업 종료를 확인합니다.

```bash
docker compose stop app
docker compose exec -T dotori-orchestrator python manage.py wait_for_document_processing --timeout 300
```

명령이 성공하면 나머지 처리 서비스를 중지합니다. 실패하면 큐를 삭제하거나 DB 작업 상태를 임의로 바꾸지 말고 원인을 확인합니다.

```bash
docker compose stop dotori-orchestrator parser-executor embedding-executor
```

이 시점에 [백업 절차](./backup-and-storage.md)에 따라 DB·원본·설정을 함께 보관합니다. 이전 버전 task 이름이 남은 큐를 새 코드로 그대로 소비하지 않도록 해야 합니다.

## 3. App부터 교체

```bash
docker compose up -d --no-deps --no-build --force-recreate app
docker compose logs --tail=100 app
docker compose ps
```

App 시작 절차가 `collapse_migration_history`와 `migrate`를 실행합니다. migration 오류가 없고 App이 `healthy`가 된 것을 확인합니다. 실패한 상태에서 다른 프로세스를 올리지 않습니다.

## 4. 배포 계획 적용과 처리 서비스 시작

저장된 설치 구성으로 배포 계획을 작성하고 필요한 서비스를 시작합니다.

```bash
python install.py --run
```

이 명령은 이미지를 빌드하지 않으며 저장된 모델 설정을 사용합니다. 배포 계획을 읽고 있는 App에도 반영되도록 다음을 실행합니다.

```bash
docker compose up -d --no-deps --no-build --force-recreate app
```

다시 App의 정상 시작을 확인합니다. 외부 HTTPS를 사용하던 설치에서는 일반 시작이 로컬 접속으로 돌아오므로, 인증서·로그인 설정을 확인하고 기존 [외부 접속 연결](../install/installation-guide.md#외부-접속-연결-선택)을 다시 적용합니다.

`python install.py --status`는 확인 명령이며 배포 계획을 갱신하지 않습니다. 관리형 로컬 LLM은 호스트 관리자가 다루므로 별도의 가상 RAG worker를 추가하거나 일반 Compose 서비스처럼 재시작하지 않습니다.

## 5. 사용 재개

모든 필요한 서비스가 정상인지 확인한 뒤 기존 파일·대화와 원본 열기를 확인합니다. [첫 문서 가이드](../use/first-document.md)로 새 문서의 처리·검색·질의응답까지 확인하고 접수를 재개합니다.

문제가 있으면 새 쓰기를 중지하고 로그를 보존합니다. 되돌리기는 이전 이미지뿐 아니라 백업한 DB·설정을 함께 사용하는 절차입니다. 원본까지 변경됐다면 같은 시점의 파일 사본도 필요합니다.
