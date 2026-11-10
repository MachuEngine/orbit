# SPIKE-002: PostgreSQL RLS + SQLAlchemy 연결 풀 실험

스파이크 코드다. 제품 코드로 쓰지 않는다. 실험 설계와 판정 기준은 `docs/spikes/SPIKE-002-rls-sqlalchemy-pool.md`를 따른다.

## 실행

```bash
cd spikes/002-rls-sqlalchemy
docker compose up -d --wait
uv sync
uv run python run_scenarios.py --repeat 3   # 검증 시나리오(R1, R2, R3, R4, R6)가 한 번이라도 기대와 다르면 종료 코드 1 (R7·R8 복구 검증이 실패해도 즉시 중단하고 종료 코드 1)
docker compose down
```

DB 비밀번호는 `ORBIT_SPIKE_PG_PASSWORD`(기본 `spike`)로 바꾼다. 볼륨이 없어 `down` 하면 DB가 사라진다. 실행할 때마다 역할, 테이블, 데이터, HNSW 인덱스를 처음부터 다시 만든다.

## 파일

- `compose.yml`: PostgreSQL(`pgvector/pgvector:0.8.0-pg17`, 호스트 포트 55433)
- `common.py`: 접속 정보, 역할(`orbit_owner`, `orbit_app`)·테이블·정책·시드 데이터·HNSW 인덱스 준비, 예외 기록 도우미
- `run_scenarios.py`: R1~R9 실행. 워크스페이스 설정(`set_config`)은 이벤트 훅 없이 각 시나리오에서 명시적으로 호출한다. 회차별 표와 시나리오별 분포 요약을 출력하고 원자료를 `results.json`에 저장한다.

## 변형과 대조군

기존 시나리오 절차는 그대로 두고 아래를 추가로 기록한다.

- **R2·R3 거부 원인 검증**: 거부는 `sqlstate == 42501` 이고 메시지에 `row-level security policy`가 있을 때만 인정한다. 다른 예외는 FAIL이다. 대조군으로 같은 방식(A 설정)의 A 행 삽입(커밋)과 그 행 수정 1행을 확인한다. 대조군이 실패하면 그 회차의 R2·R3는 INVALID(FAIL)다. 대조군 행은 확인 후 소유자가 삭제하고 행 수를 다시 확인한다. 실제 예외 메시지 첫 줄은 분포 요약에 나온다.
- **R4·R5 원인 분리**: 기본 엔진과 `pool_reset_on_return=None` 엔진으로 각각 수행한다. `Connection` 경유 변형(`[reset=None, Connection 경유]`)은 SQLAlchemy `Connection` 이 반환 전에 트랜잭션을 끝내므로 풀 reset 효과를 분리하지 못한다. 그래서 `eng.raw_connection()` 으로 DBAPI 연결을 얻어 `set_config` 후 커밋·롤백 없이 `close()` 하는 경로(`열린 트랜잭션 반환`)를 따로 만들고, 풀 `reset`·`checkin` 이벤트 호출 횟수와 그 시점의 libpq 트랜잭션 상태, 다음 요청이 같은 pid 에서 보는 설정·행 수를 기록한다. 트랜잭션 범위(R4)와 세션 범위(R5) 모두 수행한다. SQLAlchemy 2.1.8 에서 `reset` 이벤트는 `reset_on_return=None` 이어도 호출되고 실제 rollback 만 생략된다. 그래서 효과 여부는 호출 횟수가 아니라 reset·checkin 시점 상태(INTRANS/IDLE)와 요청 2 의 관찰 결과로 읽는다. 판정은 R4 기본 엔진(Connection 경유) 기준이고 나머지는 관찰이다.
- **R6 증거와 음성 대조군**: pid별 요청 수와 pid별 A↔B 전환 횟수를 기록한다. 음성 대조군(판정 제외, 관찰)은 같은 하네스를 세션 범위 설정으로 돌린 것(`neg_session_same_harness`)이다. 기존 하네스는 모든 요청이 읽기 전에 설정하므로 세션 범위여도 노출이 안 생길 수 있다. 그래서 설정을 건너뛰는 요청을 25% 섞은 하네스를 트랜잭션 범위(`mix_skip25_txn_scope`)와 세션 범위(`neg_mix_skip25_session_scope`)로 한 번씩 더 돌린다. 설정 없는 요청이 행을 보면 교차 노출로 센다. R6 판정에는 두 조건이 더해진다. (1) `mix_skip25_txn_scope` sync·async 모두 `cross_rows`·`wrong_count`·`errors` 가 0 이어야 한다. (2) `neg_mix_skip25_session_scope` sync·async 모두 `cross_rows > 0` 이어야 한다. 아니면 하네스 민감도 미증명으로 R6 를 INVALID(FAIL 처리)한다. 시드와 요청 수는 고정이지만 스레드 스케줄은 비결정적이어서 민감도는 매 회차 확인한다.
- **R7 정책 대상 변형**: 정책을 `TO PUBLIC`(대상 지정 없음)으로 바꿔 FORCE + A 설정 시 소유자가 A 행을 보는지 기록한 뒤 원래 정책으로 되돌린다. R7·R8 끝에 FORCE 해제, 정책 정의 원상태, A 설정 시 40행을 확인하고 다르면 중단한다.
- **R9 원인 측정**: `hnsw.ef_search` 값을 기록하고, 소유자 연결(RLS 우회)에서 인덱스 스캔을 강제해 같은 쿼리 벡터의 상위 `ef_search`개 중 S 행 수를 센다. R9 엔진은 psycopg `prepare_threshold=None`(서버 측 준비문장 끔)이고, `EXPLAIN (ANALYZE, FORMAT JSON)`으로 실제 실행 계획의 인덱스 사용 여부를 판정한다. 모든 변형이 같은 쿼리 벡터를 쓰는지 md5로 확인한다. 소유자 측정은 시작 시 `relforcerowsecurity=false` 이고 연결 역할이 테이블 소유자임을 확인하며 아니면 `RecoveryError` 로 중단하고, `returned == ef_search` 여부를 기록한다. 인덱스 노드의 `Rows Removed by Filter` 를 변형별로 기록하고, S/off/seqscan_off 에서 이 값이 소유자 측정의 상위 `ef_search` 중 비-S 행 수와 같은지 한 줄(`대조 S/off/seqscan_off`)로 출력한다.

데이터는 워크스페이스 A·B 각 40행, L 5,000행, S 30행이고 임베딩은 고정 시드 난수다. R7·R8은 FORCE 설정과 정책을 바꾸지만 시나리오가 끝나면 원상 복구한다.

`results.json`은 실행 결과물이며 커밋 대상이 아니다.
