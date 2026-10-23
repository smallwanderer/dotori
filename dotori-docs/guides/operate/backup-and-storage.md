# 백업과 데이터 보관

> 이전: [운영 가이드](./operation-guide.md) · 다음: [업데이트 가이드](./upgrading.md)

원본과 처리 결과를 복원하려면 DB와 파일 저장소, 설정을 함께 보관해야 합니다. Docker 이미지나 빌드 캐시만 보관하는 것으로 데이터 백업을 대신할 수는 없습니다.

## 무엇을 보관하나요

| 대상 | 기본 위치와 내용 |
| --- | --- |
| PostgreSQL | Compose의 `postgres-data` 볼륨입니다. 계정·작업공간·파일 메타데이터·벡터·대화·작업 기록을 보관합니다 |
| 원본 파일 | 호스트의 `data/uploads/`입니다. DB의 파일 참조와 함께 보관합니다 |
| 실행 설정 | `.env`와 `data/config/`입니다. 모델 선택·배포 계획·외부 연결 설정을 포함합니다 |
| 평가 자료 | `data/evaluation/`을 사용한다면 함께 보관합니다 |
| 모델 파일 | 실제 모델 캐시·다운로드 위치입니다. 위치는 서비스 mount와 runtime 설정에서 확인합니다. 오프라인 복원에는 필요합니다 |
| 이미지·빌드 캐시 | 실행 패키지와 재빌드용 자료입니다. 원본·DB의 백업이 아닙니다 |

설정에는 비밀번호와 토큰이 포함될 수 있으므로 저장소에 커밋하지 않고 접근을 제한합니다. 같은 드라이브의 사본만으로는 드라이브 장애에 대비할 수 없습니다.

## 백업 만들기

새 업로드와 설정 변경을 중지하고 진행 중 작업을 마친 뒤 DB와 파일 사본을 만듭니다. 업데이트 중이라면 [업데이트 가이드](./upgrading.md)의 중지 순서를 따릅니다.

아래 명령은 저장소 루트에서 실행합니다. DB 안에서 환경변수를 읽고 파일을 생성하므로 호스트 셸이 `.env`를 읽고 있는지와 관계없이 사용할 수 있습니다.

```bash
mkdir backups

docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/dotori-backup.dump'
docker compose exec -T db pg_restore --list /tmp/dotori-backup.dump
docker compose cp db:/tmp/dotori-backup.dump backups/database.dump
```

기존 `backups` 폴더가 있으면 새 폴더 이름을 사용합니다. 각 명령이 성공했는지 확인하고, `.env`, `data/config`, `data/uploads`와 필요한 평가 자료를 같은 백업 묶음에 복사합니다. 모델·설정을 변경하지 않은 상태에서 복사해야 DB와 파일의 시점 차이를 줄일 수 있습니다.

`pg_restore --list` 성공은 백업 목록을 읽을 수 있다는 검사입니다. 실제 복원 가능 여부는 별도의 시험 DB에 복원하여 확인합니다.

## 복원할 때

복원 대상 버전의 이미지와 설정을 준비하고 쓰기 요청을 중지한 상태에서 진행합니다. DB와 원본 파일을 같은 시점의 백업으로 복원하고, 파일 목록·원본 열기·검색을 확인합니다. 새 버전으로 변경된 DB에 이전 이미지만 연결하는 것은 복원이 아닙니다.

운영 DB를 덮어쓰기 전에 별도 시험 환경에서 절차를 확인합니다. `docker compose down -v`, 볼륨 삭제와 Docker Desktop의 Clean/Purge는 백업 명령이 아닙니다.
