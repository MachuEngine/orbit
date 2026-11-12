# ADR-0008 — Connector 계약 v0과 Sim Provider 구조

- 상태: 채택 (2026-11-11, 개발자가 단계별 진행을 위임한 범위에서 메인 세션이 결정. 이의가 있으면 2단계 PR 리뷰 때 바꾼다)
- 작성일: 2026-11-11
- 근거: ADR-0001(Connector 경계), ADR-0003(결과 불명 시 대조), SPIKE-005(초안 발송), 세부 설계 D1-1~D1-6·D5-4·D6-1 (비공개, `private-docs/`)

## 배경

2단계는 메일·캘린더 제공자를 감싸는 계약과, 그 계약을 구현하는 인메모리 시뮬레이터(Sim Provider)를 만든다. 이후 평가(3단계)와 쓰기 경로(4단계)는 모두 이 계약 위에서 동작하고, 6단계의 Google Adapter도 같은 계약 테스트를 통과해야 한다. 계약은 나중에 바꾸기 비싸므로 메서드, 오류, 장애 주입 방식을 먼저 고정한다.

## 결정

### 1. 공통
- 계약은 `typing.Protocol`로 정의하고, 모든 메서드는 **비동기**(`async def`)다. 실제 어댑터는 HTTP를 쓰고 API·워커가 비동기이기 때문이다.
- 계약 인스턴스는 **연결 하나(계정 하나)**에 묶인다. 워크스페이스·권한 판단은 계약 밖(서비스 계층)에서 한다.
- 모델은 불변(frozen) 값 객체로 둔다. 시각은 모두 시간대 정보가 있는 `datetime`이다.
- 모델에 넘기는 작업 집합 핸들(E1, M3)은 계약의 책임이 아니다(D8-1, 서비스 계층).

### 2. 오류
| 오류 | 의미 | 호출자가 할 일 |
| --- | --- | --- |
| `NotFound` | 대상이 없다 | 그대로 보고 |
| `AlreadyExists` | 클라이언트가 지정한 ID가 이미 있다 | 대조: 같은 실행으로 이미 만들어졌는지 확인 |
| `PreconditionFailed` | `if_match` 버전이 현재 버전과 다르다 | 현재 상태를 다시 읽고 판단 |
| `RateLimited(retry_after_s)` | 호출 한도 초과 | 지정 시간 뒤 재시도 가능(읽기만 자동 재시도) |
| `PermissionRevoked` | 연결 권한이 철회됐다 | 사용자에게 재연결 요청 |
| `InvalidQuery` | 지원하지 않는 검색 문법 | 모델에 오류로 돌려준다 |
| `TransientError` | 일시 오류, 쓰기는 적용되지 않았음이 확실 | 재시도 가능 |
| `ResultUnknown` | 쓰기 요청을 보냈지만 결과를 모른다(응답 유실) | **재호출 금지**, 대조 절차로 판단 (ADR-0003) |

### 3. CalendarConnector
| 메서드 | 설명 |
| --- | --- |
| `list_calendars()` | 계정의 캘린더 목록 |
| `list_events(calendar_id, time_min, time_max, *, query=None, include_cancelled=False)` | 기간 안의 일정, 시작 시각 순 |
| `get_event(calendar_id, event_id)` | 일정 1개 |
| `create_event(calendar_id, event_id, draft)` | **클라이언트가 정한 `event_id`**로 생성. 이미 있으면 `AlreadyExists`. ID는 Google 규칙(base32hex 소문자 `[a-v0-9]`, 5~1024자)을 따르며 UUID의 하이픈 없는 16진 표기를 쓴다 (D5-4) |
| `update_event(calendar_id, event_id, patch, *, if_match)` | `if_match`가 현재 `version`과 다르면 `PreconditionFailed` |
| `cancel_event(calendar_id, event_id, *, if_match)` | 상태를 `cancelled`로 바꾼다(삭제 대신) |

`CalendarEvent`: `id`, `calendar_id`, `title`, `description`, `location`, `start`, `end`, `all_day`, `attendees`(이메일·이름·응답 상태), `organizer`, `status`(`confirmed`/`cancelled`), `version`(쓰기마다 바뀌는 불투명 문자열), `updated_at`.

### 4. MailConnector
| 메서드 | 설명 |
| --- | --- |
| `search_messages(query, *, max_results=20, page_token=None)` | Gmail 검색 문법의 부분집합(아래). 지원하지 않는 연산자는 `InvalidQuery` |
| `get_message(message_id)` | 메시지 1개(헤더, 본문 텍스트, 첨부 메타데이터, 라벨) |
| `get_thread(thread_id)` | 스레드의 메시지 목록 |
| `create_draft(spec)` | 초안 생성. `Draft(draft_id, message_id, thread_id)` 반환. `spec`에 `X-Orbit-Execution-Id` 헤더 포함 |
| `get_draft(draft_id)` | 초안 조회. 발송됐거나 삭제됐으면 `NotFound` |
| `send_draft(draft_id)` | 초안 발송. 발송된 메일(`SENT` 라벨) 반환. 이미 발송된 초안이면 `NotFound`(같은 초안은 두 번 발송되지 않는다, SPIKE-005) |
| `delete_draft(draft_id)` | 초안 삭제 |

- **직접 발송 메서드는 두지 않는다.** 모든 발송은 초안을 거쳐야 결과를 대조할 수 있다(SPIKE-005).
- **전달(forward) 메서드는 두지 않는다**(D6-1, 유출 경로 축소).
- 발송된 메일의 메시지 ID는 초안의 메시지 ID와 다를 수 있고, **스레드 ID는 같다**(SPIKE-005 D4). 계약 테스트는 이 성질만 보장하고 ID 일치를 가정하지 않는다.

**검색 부분집합 (D1-6)**: `from:`, `to:`, `subject:`, `after:YYYY/MM/DD`, `before:YYYY/MM/DD`, `has:attachment`, `in:sent`·`in:inbox`·`in:drafts`, 일반 키워드, 큰따옴표 구문. 조건은 모두 AND, 대소문자 무시. 그 외 연산자(`OR`, `-`, `label:`, `rfc822msgid:` 등)는 `InvalidQuery`. Agent에게 주는 도구 설명도 이 부분집합으로 제한한다.

추가(2026-11-12, ADR-0019 §2): 메일 색인 동기화를 위해 `list_labels() -> list[MailLabel]`, `list_message_ids(label_id, *, after, page_token) -> MessageIdPage`, `current_history_id() -> str`, `list_history(start_history_id, *, page_token) -> HistoryPage`를 추가한다. 이력 위치가 만료되면 `HistoryExpired`를 던진다. 새 OAuth 권한은 필요 없다(`gmail.readonly`).

추가(2026-11-12, ADR-0020 §2): 문서 계약 `DriveConnector`(`search_files`, `get_file`, `read_file_text`)를 둔다. 검색 조건은 구조화된 값(텍스트, 종류, 수정일 범위)이고 어댑터가 Drive `q` 문법으로 바꾼다. 링크 필드는 계약에 없다. 미지원 형식은 `UnsupportedFile`, 범위 부족은 사유 `scope_missing`. 읽기 전용이다.

### 5. Sim Provider
- **SimWorld**: JSON 스냅샷(스키마 버전 포함)에서 읽는다. 계정별 메일함(메시지·스레드·초안)과 캘린더(일정)를 가진다. 스냅샷은 데이터셋 버전과 함께 Git으로 관리한다 (D1-2).
- **에피소드**: `world.fork()`로 독립 복사본을 만든다. 한 에피소드의 쓰기가 다른 에피소드나 원본에 섞이지 않는다.
- **결정성**: 시각은 주입한 `Clock`에서만 얻는다. Sim이 만드는 ID(메시지 ID, 초안 ID, 버전)는 에피소드 시드 기반으로 결정적이다. 같은 스냅샷·시드·호출 순서면 결과가 같다 (D1-5).
- **상태 비교**: `diff(before, after)`가 구조화된 변경 목록(생성·수정·취소된 일정, 생성·발송·삭제된 초안)을 돌려준다. Sim이 생성하는 ID·시각처럼 비결정적일 수 있는 필드는 정규화 규칙으로 비교에서 제외한다 (D1-3).
- **호출 기록**: 모든 계약 호출을 (도구 이름, 인자, 결과 또는 오류) 순서대로 기록한다. 궤적 채점에 쓴다 (D1-3).
- **FaultPlan**: 선언형 규칙 목록(`tool`, `nth_call` 또는 `match`, `error`, `retry_after_s`). 지원 오류는 위 오류 표 전체이며, 특히 `result_unknown`은 **쓰기를 적용한 뒤** `ResultUnknown`을 던진다(응답 유실 재현). `result_unknown_not_applied`는 적용하지 않고 같은 오류를 던진다. 두 경우를 모두 재현해야 대조 로직을 검증할 수 있다 (D1-4).

### 6. 계약 테스트
- `backend/tests/contract/`에 메일·캘린더 계약 테스트 묶음을 둔다. 구현체 팩토리를 매개변수로 받아, 지금은 Sim에 대해 실행하고 6단계에서 Google Adapter(테스트 계정, 명시적 선택 시에만)에도 같은 묶음을 실행한다.
- 계약 테스트는 위 표의 성질(클라이언트 지정 ID 중복, `if_match`, 초안 발송 후 404, 재발송 거절, 스레드 ID 유지, 검색 부분집합과 `InvalidQuery`)을 모두 검사한다.

## 검토한 대안

| 대안 | 채택하지 않은 이유 |
| --- | --- |
| 동기 계약 | 실제 어댑터의 HTTP 호출과 비동기 API·워커 구조에 맞지 않는다 |
| `send_message` 직접 발송 메서드 | 응답이 유실되면 발송 여부를 대조할 방법이 없다(SPIKE-004 불성립) |
| 일정 삭제(`delete_event`) | Google에서도 삭제는 취소 상태로 남고, 대조·감사에 취소 기록이 필요하다 |
| HTTP 수준 가짜 서버 | Adapter까지 검증하지만 구현 비용이 1인 개발 범위를 넘는다(D1-1). 차이가 문제 되면 해당 영역만 보강 |

## 결과

- 3단계 평가 러너와 4단계 쓰기 경로가 이 계약과 Sim의 FaultPlan·호출 기록·상태 비교를 그대로 쓴다.
- Google Adapter는 이 계약에 맞춰 만들고 같은 계약 테스트로 Sim과의 차이를 관리한다.
