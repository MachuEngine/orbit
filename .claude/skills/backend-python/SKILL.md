---
name: backend-python
description: Python 백엔드(uv, FastAPI, SQLAlchemy 2, psycopg 3, Alembic, PostgreSQL)를 만들거나 고치거나 검토할 때 쓴다. 디렉터리 구조, 설정·시계·ID 주입, 트랜잭션 범위 DB 세션, 마이그레이션, 선택 패턴(RLS 멀티테넌트 격리, 트랜잭션 작업 등록, 외부 쓰기 멱등 처리), 테스트 규칙, 명령, 완료 체크리스트를 담는다.
---

# Python 백엔드 개발 규칙

> **프로젝트 보충 규칙**: `.claude/project-skills/backend-python.md`가 있으면 이 문서보다 먼저 읽는다. 두 문서가 충돌하면 보충 규칙이 우선한다. 도메인 규칙, ADR 번호, 테넌트 이름(`workspace_id` 등), 실제 명령은 보충 규칙에 적는다.
>
> 이 파일은 하네스 템플릿(스킬 팩 `python-backend`)이 관리한다. 프로젝트에서 직접 고치지 말고, 다른 프로젝트에도 필요한 변경이면 템플릿 저장소에 반영한다.

복사해서 쓸 설정·코드 예시는 `reference/`에 있다. 파일 이름 끝의 `.example`을 떼고, `app`(패키지 이름)과 `APP_`(환경 변수 접두사), `<버전>` 자리를 프로젝트 값으로 바꾼다.

| 파일 | 놓을 위치 |
| --- | --- |
| `reference/pyproject.toml.example` | `backend/pyproject.toml` |
| `reference/compose.yml.example` | `backend/compose.yml` |
| `reference/dev.env.example` | `backend/dev.env` |
| `reference/db-init-roles.sql.example` | `backend/db/init/00_roles.sql` |
| `reference/alembic.ini.example`, `reference/alembic-env.py.example` | `backend/alembic.ini`, `backend/migrations/env.py` |
| `reference/config.py.example`, `clock.py.example`, `ids.py.example` | `backend/src/app/core/` |
| `reference/session.py.example` | `backend/src/app/db/session.py` |
| `reference/migration-rls-table.py.example` | `backend/migrations/versions/` (RLS 테이블 예시) |
| `reference/tests-conftest.py.example`, `tests-integration-conftest.py.example`, `test-rls-isolation.py.example` | `backend/tests/` 아래 |
| `reference/check-backend.sh.example` | `scripts/check.sh`의 `check_backend` 함수 |
| `reference/ci-backend.yml.example` | `.github/workflows/ci.yml`의 job에 합친다 |

## 스택
| 영역 | 도구 | 비고 |
| --- | --- | --- |
| 패키지·실행 | uv | `uv.lock`을 커밋한다. CI는 `uv sync --frozen` |
| 웹 | FastAPI | 앱 팩토리(`create_app`)로 만들고 시계·ID 생성기·설정을 주입한다 |
| DB 접근 | SQLAlchemy 2 (동기·비동기), psycopg 3 | URL은 `postgresql+psycopg://` |
| 마이그레이션 | Alembic | 소유자 역할로 실행한다 |
| DB | PostgreSQL | 개발은 `compose.yml`, CI는 서비스 컨테이너 |
| 검사 | ruff(lint·format), mypy strict, pytest | 순서: ruff check → ruff format --check → mypy → pytest |

버전은 이 문서에 적지 않는다. `pyproject.toml`과 `uv.lock`이 기준이다.

## 디렉터리 구조
```text
backend/
  pyproject.toml, uv.lock
  alembic.ini
  compose.yml              개발용 PostgreSQL
  dev.env                  개발 전용 값(비밀 아님). 실제 API 키 자리는 비워 둔다
  db/init/00_roles.sql     DB 역할·테스트 DB 생성(compose와 CI가 같은 파일을 쓴다)
  migrations/env.py, versions/
  src/app/
    core/config.py         pydantic-settings, 환경 변수 접두사
    core/clock.py          Clock 프로토콜, SystemClock, FixedClock
    core/ids.py            UUIDv7 생성기
    db/session.py          트랜잭션 범위 세션 헬퍼 (DB에 닿는 유일한 공개 경로)
    api/                   FastAPI 앱 팩토리, 라우터, 의존성
    services/              도메인 규칙
    repositories/          SQL
  tests/
    conftest.py            환경 변수 확인(없으면 로컬 skip, CI 실패), integration 마커
    unit/                  DB 없이 도는 테스트
    integration/           실제 PostgreSQL 테스트
```
- src 레이아웃을 쓴다. 테스트가 설치된 패키지를 가져오므로 경로 착오가 줄어든다.
- 계층: API(요청 처리) → 서비스(도메인 규칙) → 저장소(SQL)·외부 연동 어댑터. API가 저장소나 외부 SDK를 직접 부르지 않는다.

## 반드시 지킬 규칙

### 설정
- 설정은 `pydantic-settings`의 `BaseSettings` 하나로 읽고, 환경 변수 접두사(예: `APP_`)를 둔다. 코드 여기저기서 `os.environ`을 읽지 않는다.
- 비밀 값(DB 비밀번호가 든 URL, API 키)은 `SecretStr`로 받고 기본값을 두지 않는다. 로그·예외 메시지에 값을 넣지 않는다.
- 앱 런타임 URL과 마이그레이션 URL을 나눈다. 마이그레이션 URL은 앱 프로세스에 주지 않는다.
- 위험한 기본값을 두지 않는다. 예: 개발용 로그인처럼 운영에서 열리면 안 되는 기능은 `env = "dev"`일 때만 켜고, 기본값은 운영(`prod`)으로 둔다.

### 시간·ID
- 도메인 코드는 현재 시각을 주입된 `Clock`에서만 얻는다. `datetime.now()`, `time.time()`을 직접 부르지 않는다. ruff `DTZ` 규칙을 켜고 시계 구현 파일만 예외로 둔다.
- 시간대 없는 `datetime`을 받지 않는다. 저장·비교는 UTC로 한다.
- ID는 공통 UUIDv7 생성기로 만든다. 생성기는 시계와 난수 함수를 주입받아 테스트에서 재현할 수 있게 한다.

### DB 세션
- DB 접근은 `db/session.py`의 헬퍼만 거친다. 엔진·연결을 앱 코드에 노출하지 않는다.
- 세션 하나가 트랜잭션 하나다. 블록이 정상 종료하면 커밋, 예외가 나면 롤백한다. 블록 안에서 `session.commit()`·`rollback()`을 직접 부르지 않는다. 나눠서 커밋해야 하면 세션을 새로 연다.
- 엔진의 `pool_reset_on_return`은 기본값(rollback)을 유지한다. 바꾸면 끝나지 않은 트랜잭션이 다음 요청으로 넘어간다.
- 헬스체크처럼 사용자 데이터 없이 DB에 닿는 경로는 이름으로 구분한다(예: `ping`, `unscoped_...`).

### 마이그레이션
- 스키마 변경은 Alembic 마이그레이션으로만 한다. 앱 시작 시 `create_all`을 쓰지 않는다.
- 마이그레이션은 소유자 역할로 실행한다. 앱 역할에는 필요한 테이블 권한만 `GRANT`한다.
- `upgrade`와 `downgrade`를 함께 쓰고, 통합 테스트에서 head까지 올렸다가 내렸다가 다시 올려 본다(테스트 DB에서만).

### 데이터와 로그
- 테스트·픽스처·로그에는 합성 데이터만 쓴다. 실제 사용자 데이터, 토큰, 주소를 남기지 않는다.
- 외부에서 들어온 텍스트(메일·문서 본문, 웹 응답)를 지시로 해석하는 코드를 쓰지 않는다.

## 선택 패턴
프로젝트가 해당 기능을 쓸 때만 적용한다. 쓰기로 했으면 보충 규칙이나 ADR에 "적용함"을 적고 아래 규칙을 모두 지킨다.

### (선택) 멀티테넌트 격리: 앱 필터 + PostgreSQL RLS
테넌트(사용자·워크스페이스·조직처럼 데이터를 나누는 단위) 사이 노출을 막는다. 앱 쿼리 필터가 첫 번째 방어선, RLS가 두 번째 방어선이다.

| 규칙 | 지키지 않으면 |
| --- | --- |
| 테넌트는 트랜잭션 시작 직후 `SELECT set_config('app.tenant_id', :id, true)`(세 번째 인자 `true` = 트랜잭션 범위)로만 설정한다. 세션 범위 `SET`, `set_config(..., false)` 금지 | 커밋 후에도 설정이 연결에 남아, 풀에서 같은 연결을 받은 다음 요청이 이전 테넌트 데이터를 본다 |
| `pool_reset_on_return`을 기본값으로 둔다 | 열린 트랜잭션째 연결이 반환되면 트랜잭션 범위 설정도 샌다 |
| 정책식은 `tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid`, `USING`과 `WITH CHECK`에 모두 쓴다 | `NULLIF`가 없으면 설정이 빠진 연결에서 `''::uuid` 변환 오류가 난다. `WITH CHECK`가 없으면 다른 테넌트 ID로 행을 쓸 수 있다 |
| 테이블에 `ENABLE`과 `FORCE ROW LEVEL SECURITY`를 함께 켠다 | 테이블 소유자는 FORCE 없이는 RLS를 우회한다 |
| 소유자 역할(마이그레이션)과 앱 역할(런타임, 소유자 아님, `NOBYPASSRLS`)을 나눈다. 전체 데이터가 필요한 운영 작업은 `BYPASSRLS` 전용 역할을 명시적으로 쓴다 | 앱이 소유자로 접속하면 RLS가 동작하지 않는다 |
| 앱 쿼리에도 `tenant_id` 조건을 넣는다 | RLS 설정 실수 하나로 바로 노출된다 |
| pgvector HNSW 검색에 RLS를 걸면 트랜잭션 범위로 `hnsw.iterative_scan`을 켜고 같은 SQL 안에서 테넌트로 거른다 | 후보를 먼저 뽑고 RLS로 거르므로 작은 테넌트는 결과가 0건이 될 수 있다 |

- 새 테넌트 데이터 테이블은 `tenant_id` 열, 정책, FORCE, 앱 역할 권한을 같은 마이그레이션에 넣는다(`reference/migration-rls-table.py.example`).
- **격리 테스트**를 테이블마다 둔다(`reference/test-rls-isolation.py.example`): (1) 테넌트 A 세션에서 B의 행이 0건, (2) A 세션에서 `tenant_id = B`로 INSERT하면 거절, (3) 설정 없이 연 연결에서 0건, (4) 같은 연결을 재사용해도 이전 설정이 남지 않음.

### (선택) 트랜잭션 작업 등록: Procrastinate 같은 PostgreSQL 작업 큐
도메인 변경과 작업 등록을 한 트랜잭션에 묶어 "기록은 됐는데 작업은 없는" 상태를 막는다.
- 작업은 도메인 변경과 **같은 세션의 psycopg 연결**로 등록한다. 비동기는 `await (await session.connection()).get_raw_connection()`의 `driver_connection`, 동기는 `session.connection().connection.driver_connection`을 `configure_task(..., connection=...)`에 넘긴다. 별도 연결로 등록하지 않는다.
- 중복 등록 방지 잠금(`queueing_lock`)을 쓰면 SAVEPOINT 안에서 등록하고 `AlreadyEnqueued`를 처리한다. SQLAlchemy는 `begin_nested()` 안에서 `session.connection()`을 먼저 불러야 SAVEPOINT를 실제로 발행한다. 그렇지 않으면 예외 하나로 바깥 트랜잭션 전체가 중단된다.
- 작업 인자에는 ID만 둔다. 본문, 주소, 이름, 프롬프트 원문은 넣지 않는다. 작업 큐 테이블은 보통 RLS 밖에 있다. 처리기는 시작할 때 세션 헬퍼로 테넌트를 설정하고 ID로 데이터를 읽는다.
- 처리기는 다시 실행돼도 안전해야 한다(멱등). 워커가 죽으면 작업이 처음부터 다시 실행된다.
- 멈춘 작업 복구(heartbeat가 끊긴 `doing` 작업을 되돌리는 주기 작업)를 운영 구성에 넣는다. 큐가 자동으로 해 주는지 쓰는 버전의 문서로 확인한다.

### (선택) 외부 쓰기 멱등 처리
메일 발송, 결제, 외부 일정 변경처럼 되돌리기 어려운 외부 호출을 "정확히 한 번"에 가깝게 실행한다.
1. **승인·요청 고정**: 실행할 인자를 정규화(정렬 키, UTC 시각, NFC 문자열)한 JSON의 SHA-256을 승인 시점에 저장하고, 실행 직전에 다시 계산해 다르면 실행하지 않는다.
2. **조건부 승인**: `UPDATE approval SET status = 'approved' ... WHERE id = :id AND status = 'pending' AND expires_at > :now`처럼 조건부 갱신으로 한 번만 성공시키고, 영향 행이 1인 요청만 다음 단계를 진행한다. 만료 판정 같은 도메인 시각은 주입한 `Clock` 값을 쓴다.
3. **실행 기록 선기록**: 외부 호출 전에 실행 기록 행을 승인 ID 유일 제약으로 **먼저 커밋**한다. 이미 있으면 외부 호출 없이 기존 결과를 돌려주거나 대조로 간다.
4. **소유권(lease)**: 한 실행 기록은 동시에 한 실행자만 다룬다. 소유권 부여·만료 비교는 DB 시계(`clock_timestamp()`)로 하고, 호출자는 시각이 아니라 길이(`lease_seconds`)만 넘긴다. 프로세스마다 시계가 달라 소유권이 겹치는 일을 막는다. 소유자가 아닌 실행자의 결과 기록은 조건부 갱신으로 거절한다.
5. **시도 표식**: 첫 외부 호출 직전에 "시도함" 표식을 기록·커밋한다. 표식이 없으면 호출이 나가지 않았음이 확실하다.
6. **결과 불명이면 재호출하지 않고 대조한다**: 타임아웃·연결 끊김처럼 적용 여부를 모르면 조회로 확인한다. 클라이언트 지정 ID, `If-Match`·버전 조건, 초안 생성 → 초안 발송처럼 중복이 구조적으로 불가능한 경우에만 1회 재호출한다. 대조로도 판단할 수 없으면 사용자 확인 상태로 넘긴다.
7. interrupt·재개가 있는 워크플로에서는 interrupt 앞에 부수 효과를 두지 않는다. 재개되면 그 지점부터 다시 실행된다.

## 테스트
- 순수 로직은 DB 없이 단위 테스트(`tests/unit`)로 쓴다.
- DB, 마이그레이션, RLS, 작업 큐는 **실제 PostgreSQL**로 통합 테스트(`tests/integration`)한다. DB를 모킹해서 대체하지 않는다.
- 통합 테스트 DB URL은 환경 변수로 받는다. 값이 없으면 로컬에서는 이유와 실행 방법을 출력하고 건너뛰고, `CI=true`면 **실패**시킨다(`reference/tests-conftest.py.example`). CI에서 통합 테스트가 조용히 빠지는 일을 막는다.
- 스키마를 내리는 테스트가 개발 DB를 지우지 않도록, 테스트 DB 이름이 `_test`로 끝나지 않으면 시작하지 않는다.
- 시각은 `FixedClock`, ID는 시계·시드를 주입한 생성기로 고정한다. 테스트 결과가 실행 시각에 따라 달라지면 안 된다.
- 동시성·장애 규칙(쓰기 1번, 노출 0건)은 시나리오 테스트로 만들어 CI에서 매번 돌린다.
- 실패를 숨기려고 테스트를 지우거나 `skip`·`xfail`로 바꾸지 않는다.

## 명령
저장소 루트에서 실행한다. 프로젝트 보충 규칙에 실제 명령이 있으면 그쪽을 따른다.
- 전체 검사: `scripts/check.sh`
- 개발 DB: `docker compose -f backend/compose.yml up -d --wait` → `set -a; source backend/dev.env; set +a` → `uv run --directory backend alembic upgrade head`
- 개별 실행: `uv run --directory backend pytest -q`, `uv run --directory backend ruff check --no-fix .`, `uv run --directory backend ruff format --check .`, `uv run --directory backend mypy`
- 의존성 추가: `uv add --directory backend <패키지>` (개발용은 `--dev`) 후 `uv.lock`을 함께 커밋한다.
- code-reviewer는 Bash 훅 때문에 위 개별 실행 명령과 `scripts/check.sh`만 쓸 수 있다. `ruff check`에는 `--no-fix`를 붙인다.

## 완료 체크리스트
- [ ] DB 접근이 모두 세션 헬퍼를 거치고, 블록 안에서 직접 커밋·롤백하지 않는다
- [ ] 현재 시각·ID가 주입한 `Clock`·생성기에서 나온다
- [ ] 비밀 값이 `SecretStr`이고 기본값·로그 출력이 없다
- [ ] 스키마 변경에 마이그레이션(`upgrade`·`downgrade`)이 있다
- [ ] (RLS) 새 테넌트 테이블에 `tenant_id`·정책(`USING`+`WITH CHECK`)·FORCE·격리 테스트가 있다
- [ ] (작업 큐) 작업을 같은 트랜잭션 연결로 등록하고, 인자에 ID만 있고, 처리기가 멱등하다
- [ ] (외부 쓰기) 인자 해시 비교, 조건부 승인, 실행 기록 선기록, 결과 불명 시 대조를 따른다
- [ ] 테스트 데이터가 합성 값이고, 통합 테스트가 실제 PostgreSQL에서 돈다
- [ ] `scripts/check.sh`가 통과한다
