# Google Calendar 계약 테스트 실행과 확인 사항

근거: ADR-0016 4절, ADR-0008 6절. 대상은 `orbit.connectors.google.calendar.GoogleCalendarConnector`다(Gmail은 6D).

## 두 가지 실행

| 이름 | 실행 조건 | 하는 일 |
| --- | --- | --- |
| `google-mock` | 항상 | `backend/tests/contract/google_calendar_mock.py`의 HTTP 모의 서버(httpx MockTransport)를 상대로 계약 묶음 전체를 돌린다. 모의 서버는 `minimal_world` 계정의 캘린더·일정으로 시드한다. |
| `google-live` | `ORBIT_CONTRACT_GOOGLE=1`과 `ORBIT_CONTRACT_GOOGLE_ACCESS_TOKEN` | 실제 Google Calendar를 상대로 같은 묶음을 돌린다. 조건이 없으면 skip한다. |

모의 서버가 흉내 내는 범위와 못 하는 범위는 `google_calendar_mock.py` 맨 위 docstring에 있다.
모의 서버를 통과해도 아래 "실계정에서 확인할 항목"은 확인된 것이 아니다.

## 실계정 실행 방법 (6E)

1. 테스트 전용 Google 계정을 쓴다. 실제 사용자 계정과 실제 일정은 쓰지 않는다.
2. 접근 토큰을 환경 변수로 준다. 토큰은 파일·로그·커밋에 남기지 않는다.

   ```
   ORBIT_CONTRACT_GOOGLE=1 ORBIT_CONTRACT_GOOGLE_ACCESS_TOKEN=<토큰> \
     uv run --directory backend pytest tests/contract -k google-live
   ```

   `google-live`는 고정 토큰만 쓰므로 토큰이 만료되면 401로 `PermissionRevoked`가 난다.
   만료 시각 안에 끝내거나, 6B의 토큰 공급자를 연결한 뒤 갱신 경로까지 함께 확인한다.
3. **시드 조건 (현재는 충족되지 않아 live 실행이 통과하지 못한다)**: 계약 테스트
   (`test_calendar_contract.py` 위쪽 상수)는 캘린더 ID(`cal-alice-main`, `cal-alice-team`)와 일정
   ID·제목·시각(`evalice001`: 11-12 01:00~02:00 UTC 등)을 고정값으로 가정한다. Google은 캘린더 ID를
   스스로 정하므로 6E에서 이 상수를 픽스처로 바꿔 계정에서 읽거나 만들어야 한다. 팩토리 시그니처
   `(world, account_id)`로는 ID를 넘길 수 없어 conftest 변경이 필요할 수 있다(이번 작업 범위 밖).
   다른 계정의 캘린더·일정(`cal-bob-main`)이 `NotFound`가 되는 검사는 Google이 404 대신 403을 돌려줄
   수 있어 함께 확인한다.

## Sim 전용 가정 점검 결과

계약 테스트(`test_calendar_contract.py`)는 계약 메서드만 쓰고 Sim 내부 상태를 보지 않는다. 모의 서버
실행에서 Sim 전용 가정으로 실패한 테스트는 없었다. 다만 다음은 모의 서버가 Sim과 같게 동작하도록
만들어 통과시킨 것이므로 실계정에서 달라질 수 있다.

- 취소한 일정을 `get_event`·`list_events(include_cancelled=True)`가 시작·끝을 포함해 돌려준다.
- `q` 검색이 생성 직후에도 바로 일정을 찾는다.
- 취소된 일정 ID로 `create_event`를 다시 부르면 `AlreadyExists`다.
- 모르는 일정에 `if_match`와 함께 수정·취소하면 `NotFound`다.
- 다른 계정의 캘린더 접근이 `NotFound`다.

## 실계정에서 확인할 항목

2단계에서 넘긴 확인 사항(`docs/implementation-plan.md`)과의 대응이다.

| 2단계 확인 사항 | 관련 테스트·코드 | 확인할 내용 |
| --- | --- | --- |
| 검색 의미(단어 단위) | `test_list_events_query_*` | `q`가 제목의 완전한 단어를 대소문자 무관하게 찾는지. 설명·장소·참석자도 보는지(계약은 제목만 보장). |
| 생성 직후 검색 색인 지연 | `test_list_events_query_finds_whole_title_word` (생성 직후 `q` 검색) | 방금 만든 일정이 `q`로 바로 나오는지. 지연되면 이 테스트는 재시도나 대기가 필요하다. |
| 취소된 일정 수정·재취소 응답 | 어댑터는 막지 않고 그대로 PATCH한다 | 취소 일정에 `update_event`가 성공하는지, 재취소가 멱등인지, 오류면 어떤 상태·이유인지. 결과에 따라 어댑터에서 `NotFound` 등으로 정규화할지 정한다. |
| 계약 테스트 시드 | 위 "시드 조건" | 캘린더 ID를 Google이 정하는 문제. |
| `threads.get`의 초안 포함 | 6D(Gmail) 범위 | 이 문서 범위 밖. |

6C에서 추가로 생긴 확인 항목은 다음과 같다.

- 취소된 일정 응답의 시작·끝 생략 여부. 생략하면 어댑터가 `updated_at`을 자리 값으로 쓰므로,
  `include_cancelled=True` 목록의 정렬 순서와 `test_list_events_excludes_cancelled_by_default`가
  영향을 받는다.
- 취소(`status=cancelled` PATCH) 뒤 `events.get`이 200과 `cancelled`를 돌려주는지.
- `If-Match`에 `etag`(따옴표 포함)를 그대로 보내 412가 나는지, PATCH 응답의 `etag`가 바뀌는지.
- 클라이언트 지정 `id`로 만든 일정이 취소된 뒤 같은 `id`로 다시 만들면 409인지(모의 서버는 409).
- 존재하지 않는 일정을 `If-Match`와 함께 PATCH하면 404인지(412가 먼저 나올 수 있다).
- 다른 계정의 캘린더 접근이 404인지 403인지.
- `sendUpdates=none`이 참석자 있는 일정 생성·수정에서 초대 메일을 막는지.
- 종일 일정의 `date`와 UTC 00:00 변환, 반환 `start.dateTime`의 시간대 표기.
- `events.list`가 `timeMin`/`timeMax`를 겹침 기준으로 처리하는지, 길이 0인 일정의 포함 여부.
- 오류 본문의 `reason` 값(`rateLimitExceeded`, `userRateLimitExceeded`, `insufficientPermissions`)이
  어댑터의 매핑표와 같은지. 한도 초과를 일부러 만들기 어려우면 문서 기준으로만 둔다고 기록한다.
- 참석자 패치(`EventPatch.attendees`)가 배열을 통째로 교체하는지, 기존 참석자의 `responseStatus`를
  함께 보내면 보존되고 생략하면 `needsAction`으로 돌아가는지. 어댑터는 현재 일정을 읽어 같은 이메일
  참석자의 상태를 실어 보낸다(새 참석자는 생략). 이메일 대소문자 차이와 별칭 주소에서 보존이 되는지도 본다.
- 쓰기 중 HTTP 408이 나는 경우가 있는지(어댑터는 `ResultUnknown`으로 낸다).
