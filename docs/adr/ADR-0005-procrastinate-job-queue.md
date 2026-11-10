# ADR-0005 — 작업 큐: Procrastinate, 도메인 변경과 같은 트랜잭션에서 등록

- 상태: 채택 (2026-11-10, 개발자 승인)
- 작성일: 2026-11-10
- 근거: `docs/spikes/SPIKE-003-procrastinate-transactional-defer.md`, 세부 설계 D5-2·D7-5 (비공개, `private-docs/`)

## 배경

Run 실행, 동기화, 인덱싱, 알림 발송을 백그라운드 워커에서 처리한다. 세부 설계 D7-5는 PostgreSQL 기반 큐(Procrastinate)를 후보로 두고, 작업 등록을 도메인 변경과 같은 트랜잭션에 묶어 별도 Outbox 없이 "기록은 됐는데 작업은 안 들어간" 상태를 막는다고 가정했다. 이 가정을 SPIKE-003에서 확인했다.

## 결정

1. **작업 큐는 Procrastinate(PostgreSQL 기반)를 쓴다.** 추가로 운영할 브로커가 없다 (D7-5 유지).
2. **구현 조건**

| # | 조건 | 막는 문제 (스파이크 근거) |
| --- | --- | --- |
| 1 | 작업 등록은 도메인 변경과 같은 SQLAlchemy 트랜잭션의 psycopg 연결을 `configure(connection=...)`로 넘겨서 한다. 동기는 `session.connection().connection.driver_connection`, 비동기는 `get_raw_connection()`의 `driver_connection` | 별도 연결로 등록하면 도메인 행만 롤백되고 작업은 남거나 그 반대가 된다. 같은 연결이면 롤백 시 둘 다 사라진다 (Q1~Q4: pid·txid 일치, 10/10회) |
| 2 | 멈춘 작업 복구 주기 작업을 운영 구성에 포함한다: heartbeat 기준 `get_stalled_jobs` → `retry_job` | 워커가 죽으면 작업이 `doing`으로 남고 새 워커도 가져가지 않는다. Procrastinate 3.10에는 자동으로 되돌리는 기능이 없다 (Q6) |
| 3 | 작업 처리기는 멱등하게 만든다. 외부 쓰기는 ADR-0003의 실행 기록 선기록 규칙을 따른다 | 재시도된 작업은 처음부터 다시 실행되어 부수 효과가 중복된다 (Q6: 10/10회 2건) |
| 4 | `queueing_lock`으로 같은 대상의 중복 등록을 막을 때는 SAVEPOINT 안에서 등록하고 `AlreadyEnqueued`를 처리한다. SQLAlchemy에서는 `begin_nested()` 안에서 `session.connection()`을 먼저 호출해 SAVEPOINT를 실제로 발행시킨다 | SAVEPOINT 없이 `AlreadyEnqueued`가 나면 바깥 트랜잭션 전체가 abort되어 도메인 변경까지 사라진다 (Q7) |

## 검토한 대안

| 대안 | 장점 | 채택하지 않은 이유 |
| --- | --- | --- |
| Celery + Redis | 성숙한 생태계 | 브로커 운영이 늘고, DB 트랜잭션과 묶으려면 Outbox가 필요하다 |
| Outbox 테이블 + 별도 발행기 | 큐 종류와 무관하게 원자성 확보 | Procrastinate로 같은 트랜잭션 등록이 되므로 구성 요소를 늘릴 이유가 없다 |
| Temporal | 장애 복구·재시도를 엔진이 보장 | 별도 클러스터 운영이 1인 개발에 크다. 멈춘 작업 복구는 주기 작업 하나로 해결된다 |

## 결과

- 장점: Run 생성과 실행 작업 등록이 한 트랜잭션으로 묶여 "Run은 있는데 실행 작업이 없음" 상태가 생기지 않는다. 운영할 인프라가 PostgreSQL 하나다.
- 비용: 멈춘 작업 복구 주기 작업과 작업 처리기의 멱등성을 Orbit이 직접 구현해야 한다. raw 연결을 꺼내는 코드를 공통 헬퍼로 감싸야 한다.

## 미결정·후속 확인

- heartbeat 간격과 멈춤 판단 기준(스파이크는 1초·3초로 단축 측정), 복구 주기 작업의 실행 간격.
- LISTEN/NOTIFY를 켠 워커에서도 커밋 전 작업을 가져가지 않는지 실측(스파이크는 폴링 경로만 실측).
- 처리량, 다수 워커 동시 실행.

## 재검토 트리거

- 작업 처리량 때문에 DB 부하가 문제가 될 때
- Procrastinate 업그레이드로 `connection=` 옵션이나 멈춘 작업 처리 방식이 바뀔 때
- 장시간 작업의 재개·재시도 요구가 주기 작업으로 감당되지 않을 때
