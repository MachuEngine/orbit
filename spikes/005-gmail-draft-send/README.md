# SPIKE-005: Gmail 초안 발송으로 발송 결과 대조

설계와 판정 기준은 `docs/spikes/SPIKE-005-gmail-draft-send-reconcile.md`를 본다. 이 폴더는 스파이크 전용 코드이며 제품 코드로 쓰지 않는다. 배경은 `docs/spikes/SPIKE-004-gmail-header-preservation.md`에 있다.

## 주의

- **실제 메일이 인증된 계정 본인에게 발송된다.** 수신자 옵션은 없고 `users.getProfile`로 얻은 본인 주소로 고정된다.
- **실제 발송 수는 `--count` + 1통이다.** D1~D5를 `--count`회(1~5, 기본 3) 수행하고, 마지막에 D6(응답 유실 시뮬레이션) 1통을 더 보낸다. 기본값이면 4통, 최대 6통이다. 설계 문서의 "최대 3통"은 이 스파이크 코드에서 `count + 1`통 상한으로 바뀌었다.
- 같은 `draft_id`로 `drafts.send`를 다시 호출했을 때(D3) 두 번째 메일이 발송되면 즉시 실행 전체를 중단한다. 재발송 오류가 5xx·408·429·상태 불명이거나 D3 확인에서 같은 execution id 메일이 2통 이상 보이는 경우도 중단한다. 발송 이후 단계에서 예외가 나도 `round_error`를 기록하고 중단한다.
- 권한은 `gmail.compose`, `gmail.readonly` 두 개뿐이다. 이 스파이크가 만든 초안만 삭제한다(D2의 미발송 초안).
- 메일 주소는 `a***@gmail.com` 형태로 마스킹해 출력·저장한다. 토큰은 출력하지 않는다.

## 개발자가 할 일 (순서대로)

1. SPIKE-004와 같은 OAuth 클라이언트를 재사용한다. `secrets/gmail-oauth-client.json`이 이미 있으면 새로 만들 필요가 없다. OAuth 동의 화면의 테스트 사용자에 본인 주소가 들어 있어야 한다.
2. 이 폴더에서 실행한다.

```bash
cd spikes/005-gmail-draft-send
uv sync
uv run python draft_spike.py --dry-run run --count 3   # 선택: 네트워크 없이 계획과 헤더 확인
uv run python draft_spike.py run --count 3
```

3. **첫 실행에서 브라우저가 열리고 `gmail.compose` 권한에 새로 동의해야 한다.** 004 토큰(`secrets/gmail-token.json`)은 스코프가 달라서 쓰지 않고, 이 스파이크는 `secrets/gmail-token-compose.json`을 따로 만든다(`0600`). 저장된 토큰의 스코프가 위 두 개와 정확히 같지 않으면 사용하지 않고 재인증을 요구한다.

## 회차별 동작

각 회차는 D2 → D1 → D3 → D4 → D5 → D3 확인 순서이고, 단계마다 결과를 JSONL에 즉시 기록한다.

- D2: 미발송 초안 생성, `drafts.get` 3회(1초 간격), 그 초안 `drafts.delete`, 삭제 후 `drafts.get`이 404인지 기록
- D1: 초안 생성(`id`, `message.id`, `message.threadId` 기록) → `drafts.send` → 응답(`id`, `threadId`, `labelIds`) 기록 → `drafts.get`을 0.5초 간격 최대 30초 조회해 처음 404가 나온 시간 기록
- D3: 같은 `draft_id`로 `drafts.send` 재호출. 확정적 4xx(5xx·408·429 제외)는 상태 코드와 사유를 실패 결과로 기록하고 진행한다. 5xx, 408, 429, 상태 코드를 알 수 없는 오류는 서버에서 처리됐을 수 있으므로 `round_error`를 기록하고 중단한다.
- D4: 초안의 `message.id`·`threadId`와 발송 응답의 `id`·`threadId` 비교
- D5: 발송 응답 `id`로 `messages.get(format=metadata)` → `X-Orbit-Execution-Id`·Subject 비교, `SENT` 라벨 확인
- D3 확인: `[orbit-spike-005] <태그> round N` 제목으로 보낸편지함 검색(0건이면 30초까지 폴링), 결과 중 `X-Orbit-Execution-Id`가 이번 값인 메일 수(`first_matching_count`)를 센다. 5초 기다린 뒤 한 번 더 조회해 `second_matching_count`를 기록하고, 판정(`exactly_one`)은 두 번째 값으로 한다. 두 번째 값이 2 이상이면 `d3_duplicate_send_abort`를 기록하고 중단한다.
- D6(마지막 `round count+1`): 초안 생성 → `drafts.send`(응답 버림) → `reconcile()`이 초안 ID·`message.id`·`threadId`·execution id만으로 판단(`drafts.get` 404면 발송됨 추정 → `messages.get`, 안 되면 `threads.get`) → 대조 후 보낸편지함 제목 검색으로 같은 메일인지 확인. `drafts.send` 반환 시각부터 `reconcile()`의 첫 `drafts.get` 직전까지의 경과 시간을 `since_send_to_first_get_sec`로 기록한다. 판정이 `draft_still_exists_not_sent`이면 D1의 `first_404_elapsed_sec`(요약의 `d1_first_404_elapsed_sec`)와 이 값을 비교해, 초안이 아직 보이는 구간이었는지 해석한다.

`Message-ID`는 지정하지 않는다(004에서 Gmail이 덮어쓰는 것을 확인했다).

## 결과 파일

`runs/results-YYYYmmddTHHMMSS.jsonl`(이 폴더 아래)에 한 줄씩 추가된다. `runs/`는 루트 `.gitignore`로 제외된다. 중단되면 종료 코드 1이고 `round_error`(단계 `stage` 포함), `d3_second_send_succeeded_abort`, `d3_duplicate_send_abort` 줄이 남는다. 해석은 `docs/spikes/SPIKE-005-gmail-draft-send-reconcile.md`의 "결과"에 직접 적는다.

## 테스트 후 정리

1. 받은편지함·보낸편지함의 `[orbit-spike-005]` 메일을 삭제한다. 중단된 실행이면 초안 폴더에 남은 초안도 확인한다(결과 파일의 `draft_id` 참고).
2. Google 계정의 보안 페이지(https://myaccount.google.com/permissions)에서 이 앱의 접근 권한을 제거한다. 004를 더 쓰지 않으면 같이 제거한다.
3. `secrets/gmail-token-compose.json`을 삭제한다.
4. 더 쓰지 않으면 OAuth 클라이언트와 `secrets/gmail-oauth-client.json`도 삭제한다.
