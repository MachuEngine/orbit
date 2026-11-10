# SPIKE-003 — Procrastinate 작업 등록을 도메인 변경과 같은 트랜잭션으로

- 상태: 완료 (2026-11-10) — 가정 성립, 운영 조건 4개 (ADR-0005 제안)
- 관련 결정: 세부 설계 D7-5(PostgreSQL 기반 작업 큐, Procrastinate 후보), D5-2(백그라운드 워커), ADR-0002(Python)
- 기간: 1~2일
- 코드: `spikes/003-procrastinate-tx/` (스파이크용 코드이며 제품 코드로 쓰지 않는다)

## 확인할 질문

D7-5는 "작업 등록을 도메인 데이터 변경과 같은 DB 트랜잭션에 묶을 수 있으면, 별도 Outbox 없이 '기록은 됐는데 작업은 안 들어간' 상태를 막을 수 있다"고 가정했다. 예: Run 행을 만들면서 그 Run을 실행할 작업을 등록한다.

1. SQLAlchemy 세션(psycopg 3 드라이버)이 연 트랜잭션 안에서 Procrastinate 작업을 등록할 수 있는가. Procrastinate 3.10의 `configure(connection=...)`에 SQLAlchemy 연결의 psycopg 연결을 넘기는 방식으로 확인한다.
2. 그 트랜잭션을 롤백하면 도메인 행과 작업이 **함께** 사라지는가.
3. 워커가 처리 중에 죽으면 그 작업은 어떻게 되는가.

## 환경

| 항목 | 값 |
| --- | --- |
| Python | 3.12 (uv) |
| procrastinate | 3.10.0 |
| SQLAlchemy | 2.1.8 (`asyncio` 포함) |
| psycopg | 3.3.8 (`binary`, `pool`) |
| PostgreSQL | `pgvector/pgvector:0.8.0-pg17` 컨테이너, 포트 55434 |

참고: Procrastinate의 `contrib.sqlalchemy`는 psycopg2 전용이다. 이번 스파이크는 psycopg 3 기반 `PsycopgConnector`·`SyncPsycopgConnector`와 SQLAlchemy의 raw 연결(`driver_connection`)을 조합한다.

## 실험 구성

- 도메인 테이블 `run(id uuid primary key, status text)`.
- 작업 `execute_run(run_id)`: 실행되면 별도 테이블 `run_effect(run_id, pid, at)`에 1건 기록한다.
- 등록 경로: SQLAlchemy 트랜잭션에서 `run` 행 삽입 → 같은 트랜잭션의 psycopg 연결을 `configure(connection=...)`로 넘겨 `execute_run` 등록 → 커밋 또는 롤백.

## 시나리오와 판정 기준

| ID | 구분 | 절차 | 기대 결과 |
| --- | --- | --- | --- |
| Q1 | 검증 | (동기) 삽입 + 등록 → 커밋 → 워커 실행 | `run` 1행, 작업 1건, 워커가 처리해 `run_effect` 1건 |
| Q2 | 검증 | (동기) 삽입 + 등록 → 롤백 | `run` 0행, 작업 0건 |
| Q3 | 검증 | (동기) 삽입 + 등록 → 커밋 전에 예외 발생 | `run` 0행, 작업 0건 |
| Q4 | 검증 | (비동기, `AsyncSession`) Q1·Q2와 같은 절차 | 동기와 같은 결과 |
| Q5 | 검증 | 커밋 전, 다른 연결에서 작업 테이블 조회 / 워커가 이미 돌고 있는 상태에서 커밋 전 대기 | 커밋 전에는 작업이 보이지 않고 워커가 가져가지 않는다 |
| Q6 | 관찰 | 워커가 `execute_run` 처리 중(`run_effect` 기록 전, 기록 후 각각)에 SIGKILL | 작업 상태가 무엇으로 남는지, 새 워커가 다시 가져가는지, 그대로면 어떤 방법(멈춘 작업 재시도 기능 등)으로 복구하는지 기록 |
| Q7 | 관찰 | 같은 `queueing_lock`으로 두 번 등록 | `AlreadyEnqueued` 발생 여부와 트랜잭션 영향 기록 (중복 Run 실행 방지에 쓸 수 있는지) |

**가정이 성립했다고 보는 조건:** Q1~Q5가 기대대로 동작하고, Q6·Q7의 동작을 설명할 수 있다.

## 결과

실행: `uv run python run_scenarios.py --repeat 10` (2026-11-10, 로컬 Docker). 10회 모두 같은 결과였다.

| ID | 구분 | 10회 결과 | 관측 |
| --- | --- | --- | --- |
| Q1 | 검증 | PASS 10/10 | (동기) `run` 1행, 작업 1건 → 워커 처리 후 `succeeded`, 부수 효과 1건. 등록 전후로 SQLAlchemy 세션과 Procrastinate가 쓴 연결의 `pg_backend_pid()`·`txid_current()`가 같았다 |
| Q2 | 검증 | PASS 10/10 | (동기) 등록 후 롤백 → `run` 0행, 작업 0건. 등록 시 job id는 반환됐다(INSERT는 실행됐다가 롤백됨) |
| Q3 | 검증 | PASS 10/10 | (동기) 커밋 전 예외 → `run` 0행, 작업 0건 |
| Q4 | 검증 | PASS 10/10 | (비동기 `AsyncSession`) Q1·Q2와 같은 결과, pid·txid 일치 |
| Q5 | 검증 | PASS 10/10 | 커밋 전에는 다른 연결에서 작업이 보이지 않는다. 0.5초 간격으로 폴링하는 워커가 3초 대기 동안 **이미 커밋된 대조 작업은 처리하고** 미커밋 작업은 가져가지 않았다. 커밋 후 처리됐다 |
| Q6 (기록 전 종료) | 관찰 | 유효 10/10 | 워커를 SIGKILL하면 작업이 `doing`으로 남는다. **새 워커도 가져가지 않는다.** heartbeat 기준으로 `get_stalled_jobs`에 잡히고, `retry_job`을 직접 호출해야 `todo`로 돌아가 처리된다. 최종 부수 효과 1건 |
| Q6 (기록 후 종료) | 관찰 | 유효 10/10 | 위와 같이 복구하면 작업이 처음부터 다시 실행되어 **부수 효과 2건(중복)** |
| Q7 | 관찰 | — | 같은 `queueing_lock`으로 두 번째 등록 시 `AlreadyEnqueued`, 그 트랜잭션은 abort 상태가 되어 같은 트랜잭션의 `run` 행도 롤백된다. SAVEPOINT로 감싸면 바깥 트랜잭션은 유지된다. 첫 등록이 커밋되기 전에 다른 연결이 같은 lock으로 등록하면 첫 트랜잭션이 끝날 때까지 **기다렸다가**, 첫 쪽이 커밋되면 `AlreadyEnqueued`, 롤백되면 성공한다 |

### 판정 근거와 전제
- 트랜잭션 공유는 pid와 txid가 등록 전후로 같다는 것으로 직접 확인했다.
- Q5는 LISTEN/NOTIFY를 끈 워커(`listen_notify=False`)로 **폴링 경로만 실측**했다. NOTIFY는 PostgreSQL 규칙상 트랜잭션이 커밋될 때만 전달되므로 NOTIFY 경로에서도 커밋 전에 워커가 깨어날 일은 없다고 추론하지만, 실측하지는 않았다.
- Q6은 실험 시간을 줄이려고 heartbeat 간격 1초, stalled 기준 3초로 짧게 잡았다. 운영 기본값에서는 복구까지 걸리는 시간이 다르다.
- "자동 복구가 없다"는 Procrastinate 3.10.0 소스에서 `get_stalled_jobs`(조회만)와 `retry_job` 외에 멈춘 작업을 자동으로 되돌리는 경로가 없음을 확인한 결과다.
- SQLAlchemy는 `begin_nested()`의 SAVEPOINT를 연결을 실제로 쓸 때 늦게 발행한다. raw 연결만 쓰면 SAVEPOINT가 만들어지지 않으므로, `begin_nested()` 안에서 `session.connection()`을 먼저 호출해야 했다.

### 하지 않은 것
- LISTEN/NOTIFY를 켠 워커의 Q5, 운영 기본 시간값에서의 Q6 복구 시간은 측정하지 않았다.
- 처리량, 다수 워커 동시 실행, 주기 작업(periodic task)은 확인하지 않았다.

## 결론과 Orbit 설계에 주는 영향

**가정은 성립한다.** SQLAlchemy 세션의 psycopg 연결을 Procrastinate `configure(connection=...)`에 넘기면, 작업 등록이 도메인 변경과 같은 트랜잭션에 묶인다. 롤백하면 둘 다 사라지고, 커밋 전에는 워커가 작업을 가져가지 않는다. 따라서 D7-5(PostgreSQL 기반 작업 큐, 별도 Outbox 없음)와 Procrastinate 후보를 유지한다.

다만 **장애 복구와 중복 실행은 Orbit이 직접 처리해야 한다.** 조건은 ADR-0005에 정리했다.

1. 작업 등록은 항상 도메인 변경과 같은 SQLAlchemy 트랜잭션의 연결로 한다 (Q1~Q5).
2. 워커가 죽으면 작업이 `doing`으로 멈춘 채 남는다. 멈춘 작업을 찾아 `retry_job`하는 주기 작업을 운영 구성에 포함한다 (Q6).
3. 재시도된 작업은 처음부터 다시 실행되므로 작업 처리기는 멱등해야 한다. 외부 쓰기는 ADR-0003의 실행 기록 선기록 규칙을 따른다 (Q6).
4. `queueing_lock`으로 같은 Run의 중복 등록을 막을 때는 SAVEPOINT로 감싸야 바깥 트랜잭션이 abort되지 않는다. SQLAlchemy에서는 SAVEPOINT 발행 시점에 주의한다 (Q7).
