# SPIKE-002 — PostgreSQL RLS + SQLAlchemy 연결 풀

- 상태: 완료 (2026-11-10) — 가정 성립, 운영 규칙 5개 (ADR-0004 제안)
- 관련 결정: 세부 설계 D4-4(앱 계층 필터 + RLS 이중 격리), D4-1(PostgreSQL + pgvector), ADR-0002(Python)
- 기간: 1~2일
- 코드: `spikes/002-rls-sqlalchemy/` (스파이크용 코드이며 제품 코드로 쓰지 않는다)

## 확인할 질문

PostgreSQL 행 단위 보안(RLS, Row Level Security: 테이블의 각 행에 접근 조건을 DB가 강제하는 기능)을 SQLAlchemy 연결 풀과 함께 쓸 수 있는가?

1. 요청마다 현재 워크스페이스를 트랜잭션 범위로 설정하면, DB가 다른 워크스페이스의 행을 조회·수정·삽입하지 못하게 막는가.
2. 연결 풀에서 연결을 재사용할 때 이전 요청의 워크스페이스 설정이 다음 요청으로 **새지 않는가**.
3. 워크스페이스를 설정하지 않은 요청은 행을 하나도 보지 못하는가(설정 누락 시 차단).
4. pgvector 유사도 검색에 RLS가 걸려도 결과가 올바른가. 결과 개수가 줄어드는 문제는 없는가.

## 환경

| 항목 | 값 |
| --- | --- |
| Python | 3.12 (uv) |
| SQLAlchemy | 2.1.8 (`asyncio` 포함) |
| psycopg | 3.3.8 (`binary`, `pool`) — 동기·비동기 모두 `postgresql+psycopg` 드라이버 |
| pgvector (Python) | 0.5.1 |
| PostgreSQL | `pgvector/pgvector:0.8.0-pg17` 컨테이너, 포트 55433 |

## 실험 구성

- **역할 두 개**: `orbit_owner`(테이블 소유·마이그레이션용), `orbit_app`(애플리케이션 접속용, 테이블 소유자 아님, `BYPASSRLS` 없음).
- **테이블**: `chunk(id uuid, workspace_id uuid, text text, embedding vector(8))`. RLS 활성화.
- **정책**: `workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid`를 `USING`과 `WITH CHECK`에 모두 적용.
- **워크스페이스 설정 방법**: 트랜잭션 시작 직후 `SELECT set_config('app.workspace_id', :ws, true)` (세 번째 인자 `true` = 트랜잭션 범위, `SET LOCAL`과 같음).
- **데이터**: 워크스페이스 A·B 각각 행을 넣는다. 벡터 검색용으로 큰 워크스페이스(L, 수천 행)와 작은 워크스페이스(S, 수십 행)를 둔다.

## 시나리오와 판정 기준

| ID | 구분 | 절차 | 기대 결과 |
| --- | --- | --- | --- |
| R1 | 검증 | `orbit_app`으로 A 설정 후 전체 조회 | A 행만 보인다 |
| R2 | 검증 | 워크스페이스 설정 없이 조회·삽입 | 조회 0행, 삽입은 거부 |
| R3 | 검증 | A 설정 상태에서 `workspace_id = B`로 삽입, B 행 수정·삭제 시도 | 삽입 거부, 수정·삭제 0행 |
| R4 | 검증 | 풀 크기 1에서 요청 1(A 설정, 커밋) → 요청 2(설정 없이 조회). 롤백·예외로 끝난 요청 뒤에도 같은 확인 | 요청 2는 0행. 이전 설정이 남지 않는다 |
| R5 | 관찰 | R4를 트랜잭션 범위가 아닌 세션 범위 설정(`set_config(..., false)` = `SET`)으로 수행 | 이전 설정이 다음 요청에 새는지 기록한다 (잘못된 사용법의 위험 측정) |
| R6 | 검증 | 스레드 여러 개(동기)와 태스크 여러 개(비동기)가 같은 풀로 A·B를 번갈아 수백 번 조회 | 모든 요청이 자기 워크스페이스 행만 본다. 교차 노출 0건 |
| R7 | 관찰 | 테이블 소유자 `orbit_owner`로 설정 없이 조회. `FORCE ROW LEVEL SECURITY` 적용 전후 비교 | 소유자가 RLS를 우회하는지, FORCE로 막히는지 기록한다 |
| R8 | 관찰 | 정책에 `NULLIF` 없이 `current_setting('app.workspace_id', true)::uuid`만 쓴 경우, 한 번 설정했다 해제된 연결에서 조회 | 오류(빈 문자열 → uuid 변환 실패)인지 0행인지 기록한다 |
| R9 | 관찰 | S 워크스페이스로 설정하고 HNSW 인덱스가 있는 상태에서 `ORDER BY embedding <-> :q LIMIT 10` | 반환 개수(10개 미만인지), 결과가 모두 S인지, 실행 계획에 인덱스를 쓰는지 기록. pgvector 0.8의 `hnsw.iterative_scan` 설정 전후 비교 |

**가정이 성립했다고 보는 조건:** R1, R2, R3, R4, R6이 기대대로 동작하고, R5·R7·R8·R9의 동작을 설명할 수 있다.

## 결과

실행: `uv run python run_scenarios.py --repeat 10` (2026-11-10, 로컬 Docker). 10회 모두 같은 결과였다. 리뷰 2회를 거치며 대조군과 변형을 추가했다(아래 "판정 근거").

| ID | 구분 | 10회 결과 | 관측 |
| --- | --- | --- | --- |
| R1 | 검증 | PASS 10/10 | A 설정 시 A 40행만 보인다 |
| R2 | 검증 | PASS 10/10 | 설정 없음 → 조회 0행. 삽입은 `42501 new row violates row-level security policy for table "chunk"`로 거부. 대조군(A 설정 후 A 삽입·수정)은 성공 |
| R3 | 검증 | PASS 10/10 | A 설정 상태에서 B 삽입·A→B 이동은 RLS 정책 위반으로 거부, B 수정·삭제 0행. 대조군 성공 |
| R4 | 검증 | PASS 10/10 | 트랜잭션 범위 설정은 commit·rollback·예외·DB 오류 어느 경우에도 같은 물리 연결(pid 동일)의 다음 요청에 남지 않는다 |
| R5 | 관찰 | — | 세션 범위 설정(`SET`)은 **commit으로 끝난 요청 뒤 다음 요청에 샌다**(다른 사용자의 40행 노출). rollback·예외 뒤에는 트랜잭션과 함께 취소되어 새지 않는다 |
| R4·R5 변형 | 관찰 | — | 트랜잭션을 연 채로 연결을 풀에 반환하면 SQLAlchemy 풀의 기본 반환 rollback이 설정을 지운다. `pool_reset_on_return=None`으로 이 rollback을 끄면 **트랜잭션 범위 설정도 다음 요청에 샌다**(40행 노출) |
| R6 | 검증 | PASS 10/10 | 동기 640회·비동기 500회(풀 4, pid 4개, pid별 A↔B 전환 수십 회)에서 교차 노출 0. 설정을 건너뛰는 요청 25%를 섞어도 트랜잭션 범위는 교차 노출 0 |
| R6 음성 대조군 | 관찰 | — | 같은 혼합 하네스를 세션 범위로 돌리면 교차 노출이 매 회차 수천 행 검출됐다. 하네스가 누출을 잡아낼 수 있음을 확인 |
| R7 | 관찰 | — | 테이블 소유자는 RLS를 우회한다(5,110행 전부). `FORCE ROW LEVEL SECURITY` 후 설정 없음 → 0행. FORCE + A 설정 시 정책이 `TO orbit_app`이면 소유자 0행(소유자에게 적용되는 정책이 없어 기본 거부), 정책이 `TO PUBLIC`이면 소유자도 A 40행 |
| R8 | 관찰 | — | 정책에 `NULLIF`가 없으면, 한 번 설정했다 해제된 연결에서 `''::uuid` 변환 오류(`22P02`)가 난다. 처음 쓰는 연결은 0행. `NULLIF`가 있으면 두 경우 모두 0행 |
| R9 | 관찰 | — | 작은 워크스페이스(S, 30행)에서 HNSW 인덱스를 쓰면 `iterative_scan=off`(기본)일 때 **0행 반환**. 인덱스가 가져온 후보 `ef_search`=40개가 모두 다른 워크스페이스(L 38, A 2)라 RLS가 전부 걸러냈다(`Rows Removed by Filter`=40, 소유자 직접 측정과 일치). `relaxed_order`·`strict_order`에서는 10행, recall 1.0(대신 863행을 걸러냄). 큰 워크스페이스(L)는 모든 설정에서 10행, recall 1.0. 이 데이터 규모에서는 플래너가 S에 대해 인덱스 대신 순차 스캔을 골라 기본 설정으로도 10행이 나왔다 |

### 판정 근거와 전제
- R2·R3 거부는 SQLSTATE `42501`과 RLS 정책 위반 문구가 모두 있을 때만 인정했다. 같은 연결에서 A 삽입·수정이 성공하는 대조군이 실패하면 그 회차를 무효로 처리하도록 했다.
- R4·R5는 `pool_size=1, max_overflow=0`으로 같은 물리 연결 재사용을 `pg_backend_pid()`로 확인했다.
- R6 판정에는 "설정 누락 요청 혼합 + 트랜잭션 범위 → 교차 노출 0"과 "같은 혼합 + 세션 범위 → 교차 노출 > 0(하네스 민감도)"을 모두 넣었다.
- R9는 서버 측 준비문장을 끄고(`prepare_threshold=None`) `EXPLAIN (ANALYZE, FORMAT JSON)`의 실제 실행 계획으로 인덱스 사용을 판정했다.

### 하지 않은 것
- 벡터 검색은 쿼리 벡터 1개, 데이터 규모 1가지(5,110행)로만 측정했다. 워크스페이스 크기별 recall·지연, `iterative_scan`의 비용은 RAG 실험에서 따로 측정해야 한다.
- 운영 부하에서의 RLS 성능(정책 조건이 붙은 쿼리 계획)은 측정하지 않았다.
- Alembic 등 마이그레이션 도구와 역할 분리를 함께 쓰는 절차는 확인하지 않았다.

## 결론과 Orbit 설계에 주는 영향

**가정은 성립한다.** PostgreSQL RLS를 SQLAlchemy 연결 풀(동기·비동기, psycopg 3)과 함께 쓸 수 있다. 트랜잭션 범위로 워크스페이스를 설정하면, 같은 연결을 재사용하는 다음 요청으로 설정이 새지 않는다. 설정을 빠뜨린 요청은 아무 행도 보지 못한다. 따라서 D4-4(앱 계층 필터 + RLS 이중 격리)를 유지한다.

다만 **설정 방법과 역할 구성을 잘못 고르면 RLS가 있어도 데이터가 샌다.** 구현할 때 지킬 규칙은 ADR-0004에 정리했다.

1. 워크스페이스는 트랜잭션 시작 직후 `set_config('app.workspace_id', :id, true)`(트랜잭션 범위)로만 설정한다. 세션 범위 `SET`은 금지한다 (R5).
2. SQLAlchemy 풀의 반환 시 rollback(`pool_reset_on_return`)을 끄지 않는다 (R4·R5 변형).
3. 정책식은 `NULLIF(current_setting('app.workspace_id', true), '')::uuid`를 쓴다 (R8).
4. 애플리케이션은 테이블 소유자가 아니고 `BYPASSRLS`가 없는 역할로 접속한다. 소유자 우회를 막으려면 `FORCE ROW LEVEL SECURITY`를 켜고, 정책 대상은 FORCE와 함께 동작하도록 정한다 (R7).
5. RLS가 걸린 벡터 검색은 HNSW 후보를 먼저 뽑고 RLS로 거르기 때문에, 작은 워크스페이스에서 결과가 비거나 줄어든다. 검색 쿼리에는 `hnsw.iterative_scan`을 트랜잭션 범위로 켠다. 그 비용과 recall은 RAG 실험에서 측정한다 (R9).
