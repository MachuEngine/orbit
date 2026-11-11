# Orbit 보충 규칙: 백엔드

> `.claude/skills/backend-python/SKILL.md`(하네스 스킬 팩 `python-backend`)에 Orbit에만 해당하는 규칙을 더한다. 둘이 충돌하면 이 문서가 우선한다. 근거는 괄호 안 ADR(`docs/adr/`)이다. D-번호는 비공개 세부 설계의 항목 번호이며 참조만 한다.

## 0. 범용 스킬의 이름을 Orbit 값으로 읽는다
| 범용 스킬 | Orbit |
| --- | --- |
| 패키지 `app`, 환경 변수 접두사 `APP_` | `orbit`, `ORBIT_` |
| 테넌트 `tenant_id`, 설정 `app.tenant_id`, `tenant_session` | `workspace_id`, `app.workspace_id`, `workspace_session` |
| 소유자 역할 `app_owner`, 앱 역할 `app_user` | `orbit_owner`, `orbit_app` (`backend/db/init/00_roles.sql`) |
| 개발 DB `app`, 테스트 DB `app_test` | `orbit`, `orbit_test` (포트 55440) |

범용 스킬의 선택 패턴 세 가지(RLS 격리, 트랜잭션 작업 등록, 외부 쓰기 멱등 처리)를 **모두 적용**한다.

## 1. 구조와 경계
- 계층: API(`orbit.api`) → 서비스·흐름(`orbit.runflow`, `orbit.assistant`) → 기록(`orbit.records`) / 동작(`orbit.actions`) / Connector(`orbit.connectors`). API는 저장소 SQL이나 Connector를 직접 부르지 않는다.
- **Connector 경계** (ADR-0001, ADR-0008): 메일·캘린더 제공자 API는 Connector 계약 구현 안에서만 호출한다. 서비스·그래프 노드가 Google API를 직접 부르지 않는다. 커넥터는 `ConnectorResolver`(워크스페이스 → 캘린더·메일)로 주입받는다. 계약 오류 표(`NotFound`, `AlreadyExists`, `PreconditionFailed`, `RateLimited`, `PermissionRevoked`, `InvalidQuery`, `TransientError`, `ResultUnknown`)의 의미를 지킨다. 직접 발송·전달 메서드는 만들지 않는다.
- **작업 집합 핸들** (ADR-0011, D8-1): 모델에는 일정·메일을 대화 범위 핸들(`E1`, `M3`)과 요약 필드로만 보인다. 제공자 ID는 모델 입력에 넣지 않고, 도구 인자의 핸들은 코드가 ID로 바꾼다. 모르는 핸들은 오류다.
- **모델 호출** (ADR-0009): 모든 LLM 호출은 `orbit.llm`의 `complete` 인터페이스로만 한다. 모델 ID는 설정의 고정 ID만 쓰고(`orbit.llm.model_id` 규칙, D9-3), 실제 응답 모델이 다르면 오류다. 생성 온도는 0. 프롬프트는 `PROMPT_VERSION` 상수로 관리하고 바꾸면 올린다.
- **외부 콘텐츠** (ADR-0011, D6-1): 메일 본문·일정 설명은 데이터 표지로 감싸고 길이 상한을 둔다. 외부 콘텐츠를 지시로 해석하는 코드를 쓰지 않는다. URL을 가져오는 기능을 만들지 않는다.

## 2. DB와 격리 (ADR-0004)
- 사용자 데이터 접근은 `orbit.db.session`의 `SyncDatabase`/`AsyncDatabase.workspace_session`만 거친다.
- 워크스페이스 없이 DB에 닿는 경로는 `ping()`과 `unscoped_auth_session_lookup()`뿐이다. 새 경로가 필요하면 이름에 `unscoped`를 넣고 리뷰에서 근거를 설명한다.
- 새 사용자 데이터 테이블: `workspace_id`, 정책 `NULLIF(current_setting('app.workspace_id', true), '')::uuid`(`USING`+`WITH CHECK`), `FORCE ROW LEVEL SECURITY`, `orbit_app` 권한, 격리 테스트(`tests/integration/test_rls.py` 형식).
- 하위 테이블은 `(workspace_id, 부모 id)` 복합 FK + `ON DELETE CASCADE`로 다른 워크스페이스 부모를 가리킬 수 없게 한다.
- 벡터 검색은 같은 SQL 안에서 워크스페이스로 거르고, 트랜잭션 범위로 `hnsw.iterative_scan`을 켠다.

## 3. RLS 밖 저장소 (ADR-0006)
- LangGraph 그래프 상태와 Procrastinate 작업 인자에는 ID·핸들·짧은 제어 값만 둔다. 본문, 주소, 이름, 프롬프트·응답 원문 금지.
- 작업 인자는 `{"run_id", "workspace_id"}`(UUID 문자열)뿐이다(`orbit.records.jobs.validate_job_args`). 처리기는 시작할 때 `workspace_session`으로 워크스페이스를 설정하고 ID로 읽는다.
- 그래프 상태는 `orbit.runflow.state.validate_state`를 거친다(허용 키·형식·길이 상한).
- 예외 메시지·로그에는 ID와 종류만 넣는다(`orbit.records.errors`).

## 4. 기록 계층 `orbit.records` (ADR-0012 §1·§3, ADR-0003 조건 1·2)
- **seq 부여**: `run_event`·`run_message`는 run 행을 `FOR NO KEY UPDATE`로 잠근 뒤 `max(seq)+1`을 넣는다. 같은 run의 추가가 커밋 순서대로 빠짐없이 이어진다.
- **승인 요청은 멱등**: `create_tool_call_with_approval`은 같은 `tool_call_id`로 다시 불러도 같은 행을 돌려준다.
- **조건부 승인**: 결정은 `status = 'pending' AND expires_at > :now` 조건부 갱신 하나뿐이다. 성공한 쪽만 run을 재개한다. 만료 판정 시각은 DB `now()`가 아니라 주입한 `Clock`이다.
- **실행 선기록**: `begin`은 `approval_id` 유일 제약으로 `pending` 행을 먼저 커밋한다. 이미 있으면 기존 행을 돌려준다. `approved`가 아닌 승인에는 행을 만들지 않는다(DB 쪽 두 번째 방어선). 각 메서드는 호출마다 새 트랜잭션을 열고 커밋한다.
- **lease**: 새 행이면 `begin`이 소유권을 준다. 재진입은 `claim`으로 얻고, 실패하면 외부 호출 없이 현재 기록을 돌려준다. `record_ref`·`finish`는 소유자이고 lease가 유효하고 상태가 `pending`·`unknown`일 때만 반영한다(아니면 예외 없이 False). lease 부여·비교는 DB `clock_timestamp()`로 하고 호출자는 길이(`lease_seconds`)만 넘긴다. `updated_at` 같은 도메인 시각은 주입 `Clock`이다.
- **잠금 순서는 항상 run → approval** (ADR-0012 §4): approval을 건드린 뒤 run을 잠그지 않는다(`orbit.runflow.repo.lock_run`). `begin`은 run 행을 `FOR KEY SHARE`로 잠가 `delete_run`과 직렬화된다. 그래도 교착(SQLSTATE `40P01`)이 나면 결정 API는 **한 번만** 다시 시도한다(`is_deadlock`).
- **run 삭제 전파** (ADR-0006 결정 3): `run` 삭제 시 하위 행은 복합 FK CASCADE로, LangGraph 체크포인트는 같은 트랜잭션에서 `thread_id = run_id`로 지운다. `pending`·`unknown` 실행이 있는 run은 대조 기록을 잃지 않도록 삭제를 거부한다. 대화·워크스페이스 삭제 경로를 만들면 반드시 이 run 삭제 함수를 거친다.

## 5. 쓰기 동작 `orbit.actions` (ADR-0012 §2·§3, ADR-0008, SPIKE-005)
- 동작 모델은 frozen pydantic 4종(`CreateEvent`, `UpdateEventTime`, `CancelEvent`, `SendMail`). 인자는 제공자 ID로 해석된 뒤의 값이고 시각은 aware만 받는다.
- **인자 해시** (`hashing.args_hash`): 동작 종류 포함, 키 정렬, UTC 시각, NFC 문자열, 이메일 소문자, 수신자·참석자 중복 제거 후 정렬(`to`·`cc`는 따로), `description`·`location`의 `None`은 빈 문자열과 같다. 승인 시점과 실행 직전에 같은 함수로 계산해 다르면 항상 거절한다.
- **출처 분류** (`provenance`, D6-1): 이메일 인자마다 `user` > `workspace_data` > `external_content` > `model` 우선순위로 분류해 `tool_call.provenance`에 남긴다. 텍스트 안 등장은 주소 경계로 찾고, 애매하면 찾지 못한 것으로 본다(보수적). `external_content`·`model`은 승인 화면 경고 대상이다.
- **실행기** (`executor.execute_approved`): 승인·만료·해시 검사 → `execution` 선기록 → 시도 표식 기록·커밋 → 외부 호출 → 결과 기록.
- **결과 불명 시 대조, 재호출 금지**. 자동 재호출은 중복이 구조적으로 불가능한 다음 경우의 **1회**뿐이다.

  | 동작 | 1회 재호출 조건 |
  | --- | --- |
  | 일정 생성 | 클라이언트 지정 `event_id`(= `execution.id` 16진 32자)로 조회해 `NotFound`일 때. `AlreadyExists`면 내용 비교(요청한 참석자가 모두 있으면 같은 내용) |
  | 일정 시각 변경·취소 | 현재 `version == before_version`(적용 안 됨)일 때 같은 `if_match`로 |
  | 메일 발송 | 초안이 남아 있고(`get_draft` 성공) 초안의 수신자·제목·본문이 승인 동작과 같을 때 `send_draft`. 초안이 `NotFound`면 발송됨 → `get_thread`의 `SENT`로 확인 |

  대조로 판단할 수 없으면 `needs_user`. 대조 중 읽기가 `ResultUnknown` 외 오류로 실패하면 결론 없이 `unknown`으로 두고 lease를 놓는다(다음 재진입에서 다시 대조).
- **만료·비승인 재진입은 읽기 대조만**: 승인이 만료됐거나 `approved`가 아니면, 실행 행이 없을 때는 거절, 행이 있고 시도 표식(`attempted`, `before_version`, `draft_attempted`·`draft_id`)이 없으면 외부 호출 없이 `failed`(`expired`·`not_approved`), 표식이 있으면 읽기 대조만 한다. 이때는 1회 재호출도 하지 않는다.
- 오류 종류는 예외 클래스 이름만 `error_kind`에 남긴다.
- 메모리 `ExecutionStore`(테스트)와 SQL 구현(`orbit.records.executions`)은 같은 Protocol을 따른다. 시그니처를 바꾸면 둘 다 바꾼다.

## 6. 실행 흐름 `orbit.runflow` (ADR-0003, ADR-0005, ADR-0012 §4·§4-1)
- **그래프 상태는 ID만**: `run_id`, step, 대기 중 `tool_call_id` 같은 제어 값만 둔다. 대화 원문과 핸들→ID 매핑은 **`run_message`에만** 있고, 노드는 진입할 때마다 `run_message`에서 메시지·작업 집합·출처 입력을 다시 만든다(`transcript`).
- **노드 분리**: `agent`(LLM 호출·읽기 도구·승인 요청 기록), `await_approval`(`interrupt`만, 부수 효과 없음, ADR-0003 조건 3), `execute`(Orbit `approval` 상태로 판단, 해시 재계산 후 실행기 호출).
- **step 멱등**: step마다 assistant `run_message`는 하나다. 이미 있으면 LLM을 다시 부르지 않고 저장된 응답을 쓴다. `tool_call` id는 (run_id, step, 호출 순번)에서 UUIDv5로 만든다. 도구 결과·이벤트는 run 행을 잠근 뒤 같은 step 기록이 있는지 확인하고 저장한다.
- **재개 판단은 Orbit 상태로** (ADR-0003 조건 4): `advance_run`은 멱등 진입점이다. run이 없거나 종료 상태거나 승인이 아직 `pending`이면 아무것도 하지 않는다. 그 밖에는 체크포인트 스냅샷을 보고 시작·재개·이어서 실행을 고른다.
- `durability="sync"`로 실행한다. 노드가 끝날 때마다 체크포인트를 커밋한다.
- **트랜잭션 등록**: `POST /runs`는 run 생성과 `execute_run` 등록을, 승인 결정은 조건부 갱신 성공과 `resume_run` 등록을 한 트랜잭션에서 한다. 승인 만료는 전역 스윕 대신, 승인 생성 트랜잭션에 만료 시각 실행 작업을 예약한다(`orbit_app`은 RLS 때문에 워크스페이스를 가로질러 훑을 수 없다).
- **재시도 전략** (`worker.RunRetryStrategy`): 실행 소유권을 못 얻었거나 대조가 끝나지 않은 경우(`ExecutionNotSettled`)는 `lease_seconds + 1`초 뒤, 상한 20회. 그 밖의 오류는 30초 간격 5회. 멈춘 작업 복구는 heartbeat 60초 기준 주기 작업이며, 워커와 따로 만든 앱으로 실행한다.
- 설정 지문(`run.config_fingerprint`): 패턴, 프롬프트 버전, 온도, 도구 정의, 모델 ID.
- 승인 대상 요약(`target`)은 승인 요청 시점에 커넥터에서 제목·현재 시각을 읽어 `provenance`에 저장한다. 제공자 ID는 담지 않고, 조회 실패는 승인 요청을 막지 않는다.

## 7. 인증 (ADR-0013 §2)
- 쿠키 세션: 토큰은 `secrets.token_urlsafe(32)`, DB에는 SHA-256 해시만. `orbit_session`(HttpOnly, SameSite=Lax, Path=/, dev가 아니면 Secure). 세션은 워크스페이스에 묶이고 만료 시각은 주입 `Clock` 기준이다. 토큰 원문은 로그·예외에 넣지 않는다.
- CSRF: 상태를 바꾸는 요청은 `X-CSRF-Token` 헤더 == `orbit_csrf` 쿠키 == 세션에 묶인 값(해시 비교)일 때만 처리한다. 실패는 403, 본문에는 원인 종류만.
- 세션 조회는 `auth_session_lookup` 함수(`SECURITY DEFINER`, 마이그레이션 0004) 하나로만 한다. 테이블을 직접 읽지 않는다. 발급·폐기는 `workspace_session`을 거친다.
- 개발용 로그인(`POST /api/dev/login`)은 `ORBIT_ENV=dev`일 때만 라우트를 등록한다. 6단계 로그인 전에는 dev가 아니면 앱이 시작되지 않는다(`ensure_dev_environment`). SSE는 같은 출처 쿠키로 인증하고 토큰을 쿼리 문자열에 넣지 않는다.

## 8. 평가 코드 `orbit.evaldata`·`orbit.evalrunner` (ADR-0009, ADR-0010)
- 정답은 코드가 계산한다. 평가 데이터는 합성 데이터만 쓴다.
- **구조화된 답 채점**: `expected_answer`가 있으면 `EpisodeOutput.structured`와 비교해 판정한다(`event_set`·`event`·`event_time`·`time_slot`, 시각은 오프셋 있는 ISO 8601을 순간으로 비교). 형식 오류는 `answer_format`, 값 불일치는 `wrong_answer`. 텍스트 사실은 `text_fact_recall` 보조 지표로만 쓴다.
- 궤적·상태 변화·사용량은 평가 대상의 보고가 아니라 Sim 호출 기록·`diff`·`UsageRecorder`에서 러너가 읽는다.
- **분할**: dev/test는 근거 아티팩트를 공유하지 않는다(연결 요소 단위 분할, `cluster_id`). 정답 지문이 같은 과제는 하나만 남긴다. test는 확인 플래그 없이 실행하지 않는다.
- **클러스터 bootstrap**: 통계는 결과 JSONL만 입력으로 받고, 과제별 성공률을 먼저 구한 뒤 `cluster_id` 단위로 복원추출한다. 짝지은 비교도 같은 클러스터를 함께 뽑는다. 클러스터가 10개 미만이면 경고와 클러스터별 값을 함께 보고한다. 시드를 고정하면 결과가 같아야 한다.
- **설정 지문**: 결과 행의 `config`(git SHA, 데이터셋 이름@버전/분할, 평가 대상, 모델 ID, 프롬프트 버전, 예산, FaultPlan)의 정렬 JSON 해시를 함께 저장한다.
- **고정 시계·시드**: 에피소드마다 월드를 포크하고(시드 = sha256(실험 시드, task_id, repeat)), 시계를 `as_of_time`으로 고정한다. `ScriptedLLM`으로 두 번 실행한 결과(시각·지연 제외)가 같아야 한다(회귀 테스트).
- 결과에 프롬프트·응답·질문 원문을 남기지 않는다. 측정하지 않은 개선 효과를 문서에 사실처럼 쓰지 않는다.

## 9. 테스트 (Orbit)
- Connector: `tests/contract/`의 계약 테스트 묶음을 Sim과 실제 어댑터에 같은 테스트로 돌린다. Sim FaultPlan의 `result_unknown`(적용 후 오류)과 `result_unknown_not_applied`(미적용 오류)를 모두 쓴다.
- 동시성·장애 규칙(쓰기 1번, 누출 0)은 스파이크 시나리오(S4 쓰기 직후 워커 종료, S7 동시 승인)를 통합 테스트로 옮겨 CI에서 매번 확인한다.
- 시각은 `FixedClock`, ID는 `Uuid7Generator(clock)`로 고정한다(`tests/integration/support.py`).

## 10. 명령 (Orbit)
- 전체 검사: 저장소 루트에서 `scripts/check.sh` (ruff check → ruff format --check → mypy → pytest, 이어서 프론트엔드)
- 개발 DB: `docker compose -f backend/compose.yml up -d --wait` → `set -a; source backend/dev.env; set +a` → `uv run --directory backend alembic upgrade head`. 기존 볼륨에 `orbit_test`가 없으면 `docker compose -f backend/compose.yml down -v` 후 다시 올린다.
- 개별 실행: `uv run --directory backend pytest -q`, `uv run --directory backend ruff check --no-fix .`, `uv run --directory backend mypy`
- 의존성 추가: `uv add --directory backend <패키지>` 후 `uv.lock`을 함께 커밋한다.

## 완료 체크리스트 (Orbit 추가분)
범용 스킬의 체크리스트에 더해 확인한다.
- [ ] 새 사용자 데이터 테이블에 `workspace_id`·RLS·FORCE·복합 FK·격리 테스트가 있다
- [ ] 외부 쓰기가 해시 비교·조건부 승인·실행 선기록·lease·결과 불명 시 대조를 따르고, 재호출은 위 표의 1회뿐이다
- [ ] 잠금 순서가 run → approval이다
- [ ] 그래프 상태·작업 인자·로그에 본문·주소·이름이 없다
- [ ] 제공자 API를 Connector 밖에서 부르지 않고, 모델에는 핸들만 보인다
- [ ] 시각·ID·모델 ID가 주입·고정 값이고, 평가 결과에 설정 지문이 있다
