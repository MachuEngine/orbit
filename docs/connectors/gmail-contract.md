# Gmail Adapter 계약 테스트

`GmailConnector`(`backend/src/orbit/connectors/google/gmail.py`)가 메일 계약(ADR-0008 4절)을 지키는지 확인하는 방법과, 모의 서버와 실제 Gmail이 달라질 수 있는 부분을 적는다. 근거는 ADR-0016 4절, SPIKE-004·005다.

## 실행 방법

| 구분 | 명령 | 조건 |
| --- | --- | --- |
| 모의 Gmail (`google-mock`) | `scripts/check.sh` 또는 `uv run --directory backend pytest tests/contract` | 항상 실행한다. 네트워크·계정이 필요 없다 |
| 실제 Gmail (`google-live`) | 아래 절차 | `ORBIT_CONTRACT_GOOGLE=1`일 때만 등록한다. 설정하지 않으면 실행되지 않는다 |

모의 서버는 `backend/tests/contract/fake_gmail.py`다. 상태가 있는 `httpx.MockTransport` 핸들러이고, Sim 월드의 계정 메일함(`minimal_world.json`)을 시드로 읽는다.

모의 서버가 구현하는 범위: `messages.list`·`get`, `threads.get`, `drafts.create`·`get`·`send`·`delete`. 발송하면 초안이 지워지고 새 메시지 ID·같은 스레드 ID로 `SENT` 메시지가 생기며, 같은 초안을 다시 발송하거나 조회하면 404다(SPIKE-005). `Message-ID`는 서버가 덮어쓴다(SPIKE-004). 검색은 `from:`·`to:`·`subject:`·`after:`·`before:`·`has:attachment`·`in:`·키워드·큰따옴표 구문을 단어 단위로 구현한다. 다른 계정으로의 배달, 색인 지연, 첨부 내용 다운로드는 흉내 내지 않는다.

### 실제 계정 실행 절차 (6E 단계에서 개발자가 수행)

1. 테스트 전용 Google 계정에 `gmail.readonly`·`gmail.compose` 범위로 동의해 액세스 토큰을 얻는다. 토큰은 `secrets/` 아래에만 두고 저장소에 넣지 않는다.
2. 테스트 계정 메일함에 계약 테스트의 시드(`tests/contract/test_mail_contract.py` 상단 주석)를 만든다. 합성 메일만 쓴다.
   - 받은편지함: `bob@example.org`가 보낸 `프로젝트 일정 문의`(2026-11-05), `분기 보고서 첨부`(2026-11-06, 첨부 `report.pdf`)
   - 보낸편지함: `bob@example.org`에게 보낸 `Re: 프로젝트 일정 문의`(같은 스레드, 2026-11-05)
   - 테스트 계정의 주소가 `alice@example.com`이 아니면 계약 테스트의 `ALICE_ADDR`를 맞춰야 한다.
3. 실행한다.

   ```bash
   export ORBIT_CONTRACT_GOOGLE=1
   export ORBIT_CONTRACT_GOOGLE_ACCESS_TOKEN=...   # 셸에서만 설정한다
   uv run --directory backend pytest tests/contract/test_mail_contract.py -k google-live
   ```

   실제 메일이 발송된다. 수신자는 계약 테스트의 `bob@example.org`(예약 도메인 `example.org`)라 외부로 배달되지 않지만, 테스트 계정이 아닌 계정으로는 실행하지 않는다. 현재 `google-live` 팩토리는 고정 액세스 토큰만 쓰며(갱신 불가) 만료 시간(약 1시간) 안에 끝내야 한다.
4. 아래 "실제 Gmail에서 달라질 수 있는 부분"을 확인해 결과를 SPIKE 형식으로 기록한다.

## 실제 Gmail에서 달라질 수 있는 부분 (6E에서 확인)

| 항목 | 모의 서버의 가정 | 실제에서 확인할 것 |
| --- | --- | --- |
| 검색 단어 단위 | 공백·구두점으로 나눈 단어의 연속 일치 | Gmail의 형태소·접두어 처리. 한국어 `subject:보고서`가 `보고서를`에도 걸리는지, `to:alice@example.com`이 주소 전체로 걸리는지 |
| 따옴표로 감싼 연산자 값 | `from:"a b"`·`subject:"일정 문의"`를 받는다 | Gmail이 연산자 값의 큰따옴표를 같은 뜻으로 받는지. 주소처럼 단순한 값은 따옴표 없이 보낸다 |
| `to:`의 cc 포함 | `To`·`Cc` 헤더를 모두 본다 | Bcc 수신자까지 걸리는지, `to:me` 동작 |
| 색인 지연 | 생성 즉시 검색된다 | 발송·초안 생성 직후 검색에 나타나기까지 걸리는 시간. 계약 테스트는 `wait_search`로 최대 10초 기다린다. 부족하면 테스트의 대기 시간을 늘리는 것이 아니라 지연 분포를 먼저 기록한다 |
| `threads.get`의 초안 포함 | 초안 메시지(`DRAFT` 라벨)를 포함한다 | 초안이 스레드 조회에 나오는지. 어댑터는 거르지 않으므로 호출자는 `DRAFT` 라벨로 거른다 |
| `-in:drafts` | 초안은 `in:drafts`일 때만 나오게 어댑터가 붙인다 | `messages.list`가 기본으로 초안을 돌려주는지, `-in:drafts`가 기대대로 거르는지 |
| 날짜 경계 | UTC 자정 | Gmail은 `after:`·`before:`를 계정 시간대로 해석한다. 시드 날짜는 시간대 경계에서 떨어져 있다 |
| 본문 줄바꿈 | 어댑터가 CRLF를 LF로 바꾸고 끝의 줄바꿈을 뗀다 | 실제 Gmail이 `drafts.get`에서 돌려준 본문이 `create_draft`에 보낸 본문과 같은지(발송 결과 대조에 쓰인다) |
| 초안의 `Message-ID`·`From` | 서버가 덮어쓴다 | 어댑터는 `Message-ID`·`From`·`Date`를 넣지 않는다. 헤더 `X-Orbit-Execution-Id` 보존은 SPIKE-005 D5에서 확인했다 |
| 답장 스레드 | `threadId`를 주면 그 스레드에 붙는다 | `In-Reply-To`·`References`와 `threadId`를 함께 줄 때 Gmail이 스레드를 그대로 쓰는지(제목이 다를 때도) |
| 오류 응답 | 모의 서버는 `{"error": {"errors": [{"reason": ...}]}}` 형식이다 | 429·403 `rateLimitExceeded`, 401, 만료·철회 때의 실제 본문. 어댑터는 사유 코드만 보고 분기한다 |

## 본문 정규화 규칙 (어댑터와 실행기 공통)

Gmail은 본문 줄바꿈을 CRLF로 저장하고 끝에 줄바꿈을 붙인다. 발송 결과 대조(ADR-0003, SPIKE-005)에서 초안 본문과 승인된 본문을 비교하므로 양쪽이 같은 규칙을 쓴다.

- 어댑터(`parse_message`): `text/plain` 본문의 CRLF·CR을 LF로 바꾸고 끝의 줄바꿈을 모두 뗀다. 그 밖의 공백은 그대로 둔다.
- 실행기(`orbit.actions.executor._same_draft`): 본문은 NFC 정규화, CRLF·CR을 LF로, 끝 줄바꿈 제거 후 비교한다. 제목은 양끝 공백 제거 후 NFC로 비교한다. 수신자·참조는 소문자 이메일 집합으로 비교한다.
- 따라서 끝 줄바꿈만 다른 본문은 같은 초안으로 보고, 중간 줄바꿈·글자·앞쪽 공백이 다르면 다른 초안으로 본다.

## 파싱 상한

- MIME 중첩 깊이 상한은 `MAX_MIME_DEPTH`(20)다. 넘는 부분은 읽지 않는다.
- 본문 텍스트 글자 수 상한은 `MAX_BODY_CHARS`(200,000)다. HTML은 변환 전에 입력도 4배까지만 읽는다.
- 둘 중 하나라도 걸리면 본문을 잘라내고 끝에 `TRUNCATED_MARK`(`[... 본문이 길어 일부를 잘랐다]`)를 붙인다.

## 공통 HTTP 구조

Calendar·Gmail 어댑터는 `backend/src/orbit/connectors/google/_http.py`의 `GoogleHttp`를 함께 쓴다. `base_url` 검증(`https://*.googleapis.com`, 테스트만 `allow_test_base_url`), 401 갱신 후 한 번 재전송, 쓰기 결과 불명 판정, 읽기 재시도, 오류 매핑이 한 곳에 있다. 토큰 공급자 Protocol은 `google/tokens.py`의 `AccessTokenProvider` 하나다. 오류 매핑은 다음과 같다.

| 응답 | 오류 |
| --- | --- |
| 401(갱신 후에도), `invalid_grant`, 403 `insufficientPermissions`·`authError` | `PermissionRevoked` |
| 429, 403 `rateLimitExceeded`·`userRateLimitExceeded` | `RateLimited` |
| 404·410 | `NotFound` |
| 검색의 400 | `InvalidQuery` |
| 그 밖의 403·4xx | 사유 코드(영숫자 40자 이내)를 담은 일반 `ConnectorError` |
| 읽기의 5xx·연결 오류·`httpx.RequestError` | `TransientError` |
| 쓰기의 408·5xx·요청 전송 뒤 오류·읽을 수 없는 성공 응답 | `ResultUnknown` |
| 쓰기의 연결 전 실패(연결 실패, 풀 대기 초과, 토큰 공급 실패) | `TransientError` |
| `send_draft`가 발송 성공 뒤 조회·파싱에 실패 | `ResultUnknown` |

## 어댑터의 알려진 한계

- 검색 결과의 각 메시지를 `messages.get(format=full)`로 하나씩 읽는다(첨부 유무 확인용). `max_results` 상한은 100이고 호출 수가 결과 수에 비례한다.
- `get_draft`가 돌려주는 `DraftSpec.reply_to_message_id`는 항상 `None`이다. 초안 메시지에서 원본 메시지 ID를 알 수 없다.
- HTML만 있는 메일의 본문은 단순 변환이라 표 레이아웃 등은 보존하지 않는다.
- 토큰 갱신·저장은 `AccessTokenProvider` 구현(6B)의 몫이다.
