---
name: backend-dev
description: Orbit 백엔드(backend/) 코드를 작성·수정·검토할 때 쓴다. DB 접근, RLS 워크스페이스 설정, 외부 쓰기 실행, 작업 큐, LangGraph 노드, Connector 구현, 테스트 작성 규칙과 완료 체크리스트를 담는다.
---

# Orbit 백엔드 개발 규칙

> 1단계(백엔드 골격)에서 도구·명령이 확정되면 "명령" 절을 갱신한다. 규칙의 근거는 괄호 안 ADR이다.

## 구조 (1단계에서 확정)
- `backend/` 아래 uv 프로젝트. 앱 코드와 테스트를 분리한다.
- 계층: API(요청 처리) → 서비스(도메인 규칙) → 저장소(DB 접근) / Connector(외부 제공자). API는 저장소·Connector를 직접 부르지 않는다.

## 반드시 지킬 규칙

### DB와 격리 (ADR-0004)
- 사용자 데이터 접근은 **반드시** 공통 세션 헬퍼를 통한다. 헬퍼가 트랜잭션 시작 직후 `set_config('app.workspace_id', :id, true)`를 실행한다. 직접 엔진·연결을 열지 않는다.
- 세션 범위 `SET`, `set_config(..., false)` 금지. 엔진의 `pool_reset_on_return`을 바꾸지 않는다.
- 앱 쿼리에도 `workspace_id` 조건을 넣는다(RLS는 두 번째 방어선).
- 새 사용자 데이터 테이블에는 `workspace_id`, RLS 정책(`NULLIF(current_setting('app.workspace_id', true), '')::uuid`, `USING`+`WITH CHECK`), `FORCE ROW LEVEL SECURITY`를 마이그레이션에 함께 넣는다.
- 벡터 검색 쿼리는 같은 SQL 안에서 워크스페이스로 거르고, 트랜잭션 범위로 `hnsw.iterative_scan`을 켠다.

### 외부 쓰기 (ADR-0003)
- 외부 호출 전에 `execution` 행을 `approval_id` 유일 제약으로 먼저 커밋한다. 이미 있으면 호출하지 않는다.
- 승인 처리는 `status = 'PENDING'` 조건부 갱신으로 한 번만 성공시키고, 성공한 쪽만 재개한다.
- 결과가 불명확하면 재호출하지 않고 대조한다. 메일은 초안 생성 → 초안 발송, 대조는 초안 ID·스레드 ID (SPIKE-005).
- interrupt가 있는 노드에서 interrupt 앞에 부수 효과를 두지 않는다.

### RLS 밖 저장소 (ADR-0006)
- LangGraph 그래프 상태와 Procrastinate 작업 인자에는 ID·핸들만 둔다. 본문, 주소, 이름, 프롬프트 원문 금지.
- 작업 처리기는 시작할 때 세션 헬퍼로 워크스페이스를 설정하고 ID로 데이터를 읽는다.
- `run` 삭제 시 체크포인트(`thread_id`)도 지운다.

### 작업 큐 (ADR-0005)
- 작업 등록은 도메인 변경과 같은 세션의 psycopg 연결로 한다(`configure(connection=...)`). 별도 연결로 등록하지 않는다.
- 작업 처리기는 재실행돼도 안전해야 한다(멱등).
- `queueing_lock` 중복 처리는 SAVEPOINT 안에서 한다(`begin_nested()` 안에서 `session.connection()` 먼저 호출).

### 시간·ID·재현성
- 현재 시각은 주입된 Clock에서만 얻는다. `datetime.now()`·`time.time()`을 도메인 코드에서 직접 부르지 않는다 (D1-5).
- ID는 공통 UUIDv7 생성기로 만든다 (D4-3).
- 모델 호출은 내부 LLM 인터페이스로만, 고정 모델 ID로 한다 (D9-2, D9-3).

### Connector (ADR-0001)
- 메일·캘린더 제공자 API는 Connector 계약 구현 안에서만 호출한다. 서비스·그래프 노드가 Google API를 직접 부르지 않는다.
- 모델에게는 작업 집합 핸들만 노출하고, 제공자 ID 변환은 서버가 한다 (D8-1).

### 데이터
- 테스트·픽스처·로그에는 합성 데이터만 쓴다. 실제 메일·주소·토큰 금지.
- 외부 콘텐츠(메일·문서 본문)를 지시로 해석하는 코드를 쓰지 않는다 (D6-1).

## 테스트 작성
- 순수 로직: 단위 테스트, DB 없이.
- DB·RLS·작업 큐: 실제 PostgreSQL(docker compose)로 통합 테스트. 모킹으로 대체하지 않는다.
- Connector: 계약 테스트 묶음을 Sim과 실제 어댑터에 같은 테스트로 돌린다.
- 동시성·장애 규칙(쓰기 1번, 누출 0)은 스파이크 시나리오를 테스트로 옮겨 CI에서 매번 확인한다.
- 실패를 숨기려고 테스트를 지우거나 건너뛰게 하지 않는다.

## 명령 (1단계 확정)
- 전체 검사: 저장소 루트에서 `scripts/check.sh` (CI와 같은 순서: ruff check → ruff format --check → mypy → pytest)
- 개발 DB: `docker compose -f backend/compose.yml up -d --wait` → `set -a; source backend/dev.env; set +a` → `uv run --directory backend alembic upgrade head`. 테스트는 `orbit_test` DB를 쓴다(이름이 `_test`로 끝나지 않으면 통합 테스트가 시작하지 않는다)
- 개별 실행: `uv run --directory backend pytest -q`, `uv run --directory backend ruff check --no-fix .`, `uv run --directory backend mypy`
- 통합 테스트는 DB 환경 변수가 없으면 로컬에서는 사유를 출력하고 건너뛰고, `CI=true`에서는 실패한다
- 의존성 추가: `uv add --directory backend <패키지>` 후 `uv.lock`을 함께 커밋한다

## 완료 체크리스트
- [ ] 새 사용자 데이터 테이블에 `workspace_id`·RLS·FORCE가 있고 격리 테스트가 있다
- [ ] DB 접근이 모두 세션 헬퍼를 거친다
- [ ] 외부 쓰기가 실행 기록 선기록·조건부 승인을 따른다
- [ ] 그래프 상태·작업 인자에 본문이 없다
- [ ] 시각·ID·모델 ID가 주입·고정 값이다
- [ ] `scripts/check.sh`가 통과한다
