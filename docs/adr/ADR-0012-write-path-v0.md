# ADR-0012 — 쓰기 경로 v0: Run·승인·실행 기록, 쓰기 도구 실행기, 워커

- 상태: 채택 (2026-11-11, 개발자가 4단계 진행을 승인한 범위에서 메인 세션이 결정. 이의가 있으면 4단계 PR 리뷰 때 바꾼다)
- 작성일: 2026-11-11
- 근거: ADR-0003(LangGraph 실행, 중복 방지 조건 1~4), ADR-0004(RLS), ADR-0005(Procrastinate 조건 1~4), ADR-0006(RLS 밖 저장소), ADR-0008(커넥터 계약·오류 표), ADR-0011(읽기 전용 경로·핸들), SPIKE-005(초안 발송), 세부 설계 D4·D5·D6-1 (비공개, `private-docs/`)

## 배경
4단계는 사용자가 승인한 외부 쓰기(일정 생성·변경·취소, 메일 발송)를 실행한다. 완료 조건은 "승인 없는 쓰기 0번, 강제 종료·동시 승인에도 외부 쓰기 정확히 1번"이다. 실제 Google 연동(6단계) 전이므로 Sim 커넥터로 실행·검증한다. 세 작업(DB 기록, 실행기, 그래프·워커·API)을 병렬로 나누기 위해 경계를 먼저 고정한다.

## 결정

### 1. 테이블 (마이그레이션 0002, 모두 `workspace_id` + RLS + FORCE, ADR-0004 정책 형식)
| 테이블 | 핵심 열 | 제약 |
| --- | --- | --- |
| `run` | id(UUIDv7), workspace_id, conversation_id, status, pattern, config_fingerprint, error_kind, created_at, updated_at | status ∈ `queued`·`running`·`waiting_approval`·`succeeded`·`failed`·`cancelled` |
| `run_event` | workspace_id, run_id, seq, type, payload(jsonb), created_at | PK(run_id, seq). 추가 전용(UPDATE·DELETE 권한 없음, 삭제는 run 삭제 전파로만) |
| `run_message` | workspace_id, run_id, seq, role, content(jsonb), created_at | PK(run_id, seq). Agent 대화 원문(프롬프트·도구 결과)의 유일한 저장소. 그래프 상태에는 넣지 않는다(ADR-0006) |
| `tool_call` | id, workspace_id, run_id, tool_name, args(jsonb, 정규화된 실제 ID 인자), args_hash, provenance(jsonb), created_at | |
| `approval` | id, workspace_id, run_id, tool_call_id, args_hash, status, expires_at, decided_at, created_at | UNIQUE(tool_call_id). status ∈ `pending`·`approved`·`rejected`·`expired`. 결정은 `WHERE status='pending' AND expires_at > now`인 조건부 갱신만 |
| `execution` | id, workspace_id, approval_id, status, external_ref(jsonb), error_kind, created_at, updated_at | UNIQUE(approval_id). status ∈ `pending`·`succeeded`·`failed`·`unknown`·`needs_user` |

- 시각은 DB `now()`가 아니라 주입한 `Clock` 값을 쓴다(만료 판정 포함). 결정성 테스트를 위해서다.
- 승인 만료 기본값은 15분(설정값, D5-5 후보값).
- `run` 삭제 시 하위 행과 LangGraph 체크포인트(`thread_id = run_id`)를 같은 작업에서 지운다(ADR-0006 결정 3).

### 2. 쓰기 동작과 인자 해시 (`orbit.actions`)
- 쓰기 동작은 pydantic frozen 모델 4종: `CreateEvent`, `UpdateEventTime`(시작·종료만 변경), `CancelEvent`, `SendMail`(새 메일 또는 스레드 답장. 수신자·제목·본문). 인자는 제공자 ID로 해석된 뒤의 값이다(핸들 해석은 4C).
- `args_hash` = 정렬 키·UTC 정규화 시각·NFC 문자열의 정규 JSON의 SHA-256. 승인 시점과 실행 직전에 같은 함수로 계산해 다르면 실행하지 않는다(D5-5).
- **인자 출처(D6-1 규칙 2)**: 수신자·참석자 이메일마다 출처를 `user`(사용자 발화), `workspace_data`(조회 결과의 구조 필드: 발신·수신·참석자), `external_content`(메일 본문·일정 설명에서만 등장), `model`(어디에도 없음)로 분류해 `tool_call.provenance`에 남긴다. 분류 함수는 `orbit.actions`가 제공하고, 입력 수집은 4C가 한다. `external_content`·`model`은 승인 화면 경고 대상이다(5단계).

### 3. 실행기 (`orbit.actions.executor`)
`execute_approved(approval, action, *, connectors, store, clock)`. 순서와 조건:
1. 승인 상태가 `approved`이고 만료 전이며, `args_hash(action) == approval.args_hash`일 때만 진행. 아니면 외부 호출 없이 거절.
2. `store.begin(approval_id)`로 `execution` 행을 `pending`으로 **먼저 커밋**한다. 이미 행이 있으면 외부 호출 없이 기존 결과를 돌려주거나, `pending`·`unknown`이면 대조로 간다(ADR-0003 조건 1).
3. 외부 호출. 결과를 `succeeded`(external_ref 포함) 또는 `failed`(적용되지 않았음이 확실한 오류: `NotFound`, `PreconditionFailed`, `PermissionRevoked`, `InvalidQuery`, `TransientError`, `RateLimited`)로 기록.
4. `ResultUnknown` 또는 행이 `pending`으로 남은 채 재진입하면 **재호출하지 않고 대조**한다.

| 동작 | 호출 | 대조 |
| --- | --- | --- |
| 일정 생성 | `create_event(event_id = execution.id 16진 32자)` | `get_event(id)`가 있으면 내용 비교 후 `succeeded`. `NotFound`면 같은 ID로 재호출해도 중복이 생기지 않으므로 1회 재호출(`AlreadyExists`면 내용 비교) |
| 일정 시각 변경·취소 | 실행 전 현재 `version`을 `external_ref.before_version`에 기록 후 `if_match=before_version`으로 호출 | 현재 값이 승인된 결과와 같으면 `succeeded`. `version == before_version`이면 적용 안 됨 → 같은 `if_match`로 1회 재호출(중복 적용 불가). 그 외 `needs_user` |
| 메일 발송 | `create_draft` → `draft_id`·`thread_id`를 `external_ref`에 기록·커밋 → `send_draft` | 발송 단계 불명: `get_draft`가 있으면 미발송 → `send_draft` 1회 재호출(발송된 초안은 재발송이 거절되므로 중복 불가, SPIKE-005). `NotFound`면 발송됨 → `get_thread`에서 `SENT` 메시지로 `succeeded`. 초안 생성 단계 불명: 초안은 발송 전이라 새 초안을 만들고 고아 초안 가능성을 기록 |

- 대조로도 판단할 수 없으면 `needs_user`. 자동 재시도는 위 표의 "1회 재호출"뿐이다.
- **실행 소유권(lease)**: 한 `execution` 행은 동시에 한 실행자만 다룬다. `begin`은 행을 만들면서 호출자에게 소유권을 준다. 기존 행(`pending`·`unknown`)에 재진입할 때는 `claim`으로 소유권을 얻어야 하고, 얻지 못하면 외부 호출 없이 현재 기록을 돌려준다. 소유권은 만료 시각을 가지며, 만료 시간은 커넥터 호출 타임아웃×(1+재호출 1회)보다 길게 잡는다. 소유자가 아닌 실행자의 `record_ref`·`finish`는 반영되지 않는다(조건부 갱신). 같은 프로세스 안의 동시 실행도 같은 규칙으로 막힌다.
- **시도 표식과 만료**: 첫 외부 쓰기 호출 직전에 `external_ref.attempted = true`(변경·취소는 `before_version`, 메일은 초안 생성 직전 표식)를 기록·커밋한다. 승인이 만료됐거나 `approved`가 아니게 된 뒤의 재진입에서 시도 표식이 없으면 외부 호출 없이 `failed`(`expired`)로 끝내고, 표식이 있으면 대조만 한다(대조 결과에 따른 1회 재호출 포함).
- **초안 재발송 전 내용 확인**: 대조 중 초안을 다시 발송하기 전에 초안의 수신자·제목·본문이 승인된 동작과 같은지 확인하고, 다르면 `needs_user`.
- `ExecutionStore`는 Protocol이다: `begin(approval_id, owner, lease_until) -> (execution, created: bool)`(새 행이면 소유권 포함), `claim(execution_id, owner, lease_until) -> bool`, `record_ref(execution_id, owner, external_ref) -> bool`, `finish(execution_id, owner, status, external_ref, error_kind) -> bool`, `get(approval_id)`. `record_ref`는 `external_ref`를 통째로 바꾼다. 4B는 메모리 구현으로 테스트하고, 4A가 SQL 구현을 만든다. 각 메서드는 호출마다 커밋한다.

### 4. 실행 흐름 (4C)
- API: `POST /runs`(run 생성 + `execute_run(run_id)` 작업 등록을 한 트랜잭션, ADR-0005 조건 1), `GET /runs/{id}/events`(SSE, `id = seq`, `Last-Event-ID` 이후 재생 후 새 이벤트 대기), `POST /approvals/{id}/decision`(조건부 갱신에 성공한 요청만 `resume_run(run_id)` 등록, ADR-0003 조건 2).
- **로그인 전(6단계) 임시 인증**: 개발 환경 설정에서만 켜지는 고정 워크스페이스 헤더. 개발 환경이 아니면 시작을 거부한다.
- 워커(Procrastinate): `execute_run`, `resume_run`, 멈춘 작업 복구 주기 작업(ADR-0005 조건 2). 작업 인자는 `run_id`만.
- 그래프(LangGraph, `PostgresSaver`): 상태는 `run_id`, 다음 단계, 대기 중인 `tool_call_id`뿐. 노드는 `run_message`에서 대화를 읽고 쓴다. 쓰기 도구 호출이 나오면 `tool_call`·`approval`을 멱등하게 기록하고(같은 키로 다시 써도 같은 결과) `interrupt`로 멈춘다(ADR-0003 조건 3). 재개 시 `approval` 상태를 Orbit 테이블에서 읽어 판단한다(조건 4).
- 모델에 주는 쓰기 도구는 일정 생성·시각 변경·취소, 메일 발송(초안 방식) 4개다. 대상 일정·스레드는 핸들로 가리키고, 수신자·참석자는 이메일 주소로 받는다. 조회 전용 의도에는 쓰기 도구를 주지 않는다는 D6-1 규칙 4의 의도 분류는 v0에서 하지 않고, 쓰기 경로 Run에만 쓰기 도구를 준다.

### 5. 작업 분할
| 작업 | 내용 | 의존 |
| --- | --- | --- |
| 4A | 마이그레이션 0002(표 1), 저장소 함수(조건부 승인, 실행 선기록, 이벤트 추가), SQL `ExecutionStore`, Procrastinate 스키마·같은 트랜잭션 등록 헬퍼·멈춘 작업 복구, RLS 격리 테스트 | 없음 |
| 4B | `orbit.actions`(동작 모델, 인자 해시, 출처 분류, 실행기, 대조), 메모리 `ExecutionStore`, Sim FaultPlan 기반 테스트(`result_unknown`, `result_unknown_not_applied`) | 없음 |
| 4C | 그래프, 워커 작업, API·SSE, S4(쓰기 직후 워커 종료)·S7(동시 승인) 통합 테스트 | 4A, 4B |

## 검토한 대안
| 대안 | 채택하지 않은 이유 |
| --- | --- |
| 대화 원문을 그래프 상태에 저장 | RLS 밖 저장, 삭제 전파 누락(ADR-0006) |
| 결과 불명 시 무조건 `needs_user` | 클라이언트 지정 ID·`if_match`·초안 재발송 거절처럼 중복이 구조적으로 불가능한 경우까지 사용자에게 넘기면 승인 피로가 커진다. 중복 불가가 계약으로 보장되는 1회 재호출만 허용한다 |
| 승인 만료 판정에 DB `now()` | 테스트에서 시각을 고정할 수 없다 |
| 메일 직접 발송 | 결과 대조 불가(SPIKE-004, ADR-0008) |

## 결과
- 실행 기록과 대조 절차가 커넥터 계약(ADR-0008)에만 의존하므로 6단계 Google Adapter에서도 그대로 쓴다. 단, 실제 Google의 `ResultUnknown` 판정(타임아웃·연결 끊김)과 생성 직후 조회 지연은 6단계에서 확인한다.
- 승인 화면(5단계)은 `tool_call.provenance`와 `approval`만 읽으면 된다.
