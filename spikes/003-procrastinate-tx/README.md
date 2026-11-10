# SPIKE-003: Procrastinate 작업 등록을 도메인 변경과 같은 트랜잭션으로

스파이크 코드다. 제품 코드로 쓰지 않는다. 실험 설계와 판정 기준은 `docs/spikes/SPIKE-003-procrastinate-transactional-defer.md`를 따른다.

## 실행

```bash
cd spikes/003-procrastinate-tx
docker compose up -d --wait
uv sync
uv run python run_scenarios.py              # 검증 시나리오(Q1~Q5)가 기대와 다르면 종료 코드 1
uv run python run_scenarios.py --repeat 3   # 3회 반복, 회차별 표와 분포 요약
docker compose down
```

DB 비밀번호는 `ORBIT_SPIKE_PG_PASSWORD`(기본 `spike`)로 바꾼다. 볼륨이 없어 `down` 하면 DB가 사라진다. Procrastinate 스키마와 도메인 테이블은 `run_scenarios.py`가 시작할 때 없으면 만든다.

## 파일

- `compose.yml`: PostgreSQL(`pgvector/pgvector:0.8.0-pg17`, 호스트 포트 55434)
- `common.py`: DSN, 스키마 적용(`SchemaManager.apply_schema`), 도메인 테이블 `run`, `run_effect`
- `tasks.py`: Procrastinate 앱(`PsycopgConnector`)과 작업 `execute_run(run_id, sleep_before, sleep_after)`. `run_effect` 기록 전후에 `MARKER effect_before`, `MARKER effect_committed`를 출력한다. `SPIKE_NO_SLEEP=1`이면 sleep을 건너뛴다.
- `worker.py`: `app.run_worker`를 별도 프로세스로 실행한다. `--no-wait`는 큐가 비면 종료한다. `--poll-interval`(`fetch_job_polling_interval`)과 `--no-listen-notify`(`listen_notify=False`)를 지원한다.
- `run_scenarios.py`: Q1~Q7을 실행해 회차별 관측값과 분포 요약을 출력한다. 시나리오마다 고유한 큐 이름을 쓰므로 서로 간섭하지 않는다. 결과는 `results.json`에 저장한다(커밋 대상 아님).

## raw 연결 꺼내기

- 동기: `session.connection().connection.driver_connection` (`psycopg.Connection`)
- 비동기: `(await (await session.connection()).get_raw_connection()).driver_connection` (`psycopg.AsyncConnection`)
- 이 연결을 `task.configure(connection=raw).defer(...)` / `.defer_async(...)`에 넘긴다.

## 측정 방식 (리뷰 반영)

- Q1·Q4 트랜잭션 공유 증거: defer 전후에 SQLAlchemy 세션과 raw 연결에서 각각 `SELECT pg_backend_pid(), txid_current()`를 실행해 pid와 txid가 같은지 기록한다. 네 값이 모두 일치해야 PASS다.
- Q5 폴링 증거: 워커를 `fetch_job_polling_interval=0.5초`, `listen_notify=False`로 띄운다(기본 폴링 간격은 5초). 커밋 전 대기는 3초(폴링 간격의 6배)다. 대기 시작 직후 같은 큐에 이미 커밋된 대조군 작업을 등록한다. 대조군이 대기 중에 처리되고 미커밋 작업은 처리되지 않아야 PASS다. 사용한 폴링 간격과 대기 시간은 결과(`poll_interval_s`, `hold_s`)에 남는다.
- Q7d·e 블로킹: 두 번째 스레드가 `defer` 호출 직전에 `threading.Event`를 set하고, 메인은 그 Event를 기다린 뒤 2초 대기하고 첫 트랜잭션을 끝낸다. "두 번째 defer 반환 시각 >= 첫 트랜잭션 종료 호출 시각"으로 판정하며, 두 시각(Event 수신 기준 상대 초)을 모두 기록한다.
