# SPIKE-004: Gmail 헤더 보존 확인

설계와 판정 기준은 `docs/spikes/SPIKE-004-gmail-header-preservation.md`를 본다. 이 폴더는 스파이크 전용 코드이며 제품 코드로 쓰지 않는다.

## 주의

- **실제 메일이 인증된 계정 본인에게 발송된다.** 수신자 옵션은 없고, `users.getProfile`로 얻은 본인 주소로 고정된다.
- 실행에는 개발자 본인의 Gmail 계정과 브라우저 OAuth 동의가 필요하다.
- 스코프는 `gmail.send`, `gmail.readonly` 두 개뿐이다.
- 메일 주소는 `a***@gmail.com` 형태로 마스킹해 출력·저장한다. 토큰은 출력하지 않는다.
- `secrets/`와 `runs/`(결과 파일 위치)는 git에서 제외된다.
- **토큰 파일(`secrets/gmail-token.json`)을 다른 도구와 공유하지 않는다.** 저장된 토큰의 스코프가 위 두 개와 정확히 같지 않으면(더 넓거나 좁으면) 사용하지 않고 재인증을 요구한다. 토큰 파일은 `0600` 권한으로 쓴다.

## 개발자가 할 일 (순서대로)

1. Google Cloud Console에서 새 프로젝트를 만든다.
2. 그 프로젝트에서 **Gmail API**를 사용 설정한다.
3. OAuth 동의 화면을 구성한다. 게시 상태는 **테스트**로 두고, **테스트 사용자**에 본인 Gmail 주소를 추가한다.
4. 사용자 인증 정보에서 **OAuth 클라이언트 ID**를 만든다. 애플리케이션 유형은 **데스크톱 앱**이다.
5. 내려받은 JSON을 저장소 루트의 `secrets/gmail-oauth-client.json`에 저장한다.
6. 이 폴더에서 실행한다.

```bash
cd spikes/004-gmail-headers
uv sync
uv run python gmail_spike.py --dry-run send        # 선택: 네트워크 없이 헤더만 확인
uv run python gmail_spike.py --dry-run run --count 3   # 선택: 라운드별 헤더 확인
uv run python gmail_spike.py run --count 3
```

처음 실행하면 브라우저가 열리고 동의 후 토큰이 `secrets/gmail-token.json`에 캐시된다. "확인되지 않은 앱" 경고는 테스트 상태 앱이므로 정상이다.

## 명령

| 명령 | 설명 |
| --- | --- |
| `send [--no-message-id]` | 본인에게 1통 발송. `--no-message-id`는 G4 |
| `inspect <gmail_id> [--message-id ID] [--execution-id ID]` | 헤더를 `format=metadata`로 조회 (G1). 기대값 인자가 있으면 비교하고, 없으면 받은 헤더만 출력한다. Subject는 출력·저장하지 않는다 |
| `search <message_id> [--execution-id ID] [--subject-tag TAG --round N] [--gmail-id ID]` | `rfc822msgid:` 검색 (G2)과 사용자 정의 헤더 값 검색 (G5). 대조군은 `--subject-tag`와 `--round`를 함께 줄 때만 `subject:"[orbit-spike] TAG round N"`로 질의한다(태그는 발송 때 출력·기록된 값). `--gmail-id`를 주면 결과에 그 메일이 있는지(`contains_sent_id`)도 기록한다 |
| `run [--count N] [--gap 초]` | G1~G5 전체. Message-ID를 지정해 N회(**1~10**, 기본 3), 지정하지 않고 1회 추가 발송. `--gap`은 0 이상의 유한한 수(`--gap=0.5` 형태, `nan`·음수 불가). 범위를 벗어나면 발송 전에 오류로 끝난다 |

공통 옵션(명령 앞에 둔다): `--dry-run`(send/run 전용), `--client-file`, `--token-file`, `--results-file`.

## 측정 방식

- Subject는 `[orbit-spike] <tag> round N`이다. `<tag>`는 실행마다 `secrets.token_hex(4)`로 새로 만드는 값이고 `uuid_a`·`uuid_b`와 무관하다. 본문에는 태그도 난수도 넣지 않는다. 이전 실행의 메일이 대조군에 섞이지 않는다.
- `Message-ID`는 `<uuid_a@orbit.invalid>`, `X-Orbit-Execution-Id`는 이와 다른 `uuid_b`다.
- 검색 함수는 개수가 아니라 메시지 id 목록을 돌려준다. 대조군·G2/G3는 방금 보낸 `gmail_id`가 결과에 들어 있을 때만 found로 판정하고, `count`와 `contains_sent_id`를 모두 기록한다. G5 질의도 `contains_sent_id`를 기록한다.
- G5는 `uuid_b`로만 검색한다. 대조군은 Subject 검색이며, 보낸 메일이 잡힐 때까지 최대 120초 폴링한 뒤 G5를 실행한다. 대조군이 끝내 잡히지 않으면 `g5_valid: false`다. 보낸 메일이 결과에 없는 질의는 30초 뒤 한 번 더 확인해 둘 다 기록한다. 추가로 `uuid_a`를 `rfc822msgid:` 없이 검색해 Message-ID가 일반 색인 대상인지 본다.
- G3의 `t0`는 발송 응답 수신 직후이며, G3 폴링을 G1 조회보다 먼저 한다. G2/G3 결과에는 `count_is_1`(결과가 정확히 1건인지)도 기록한다.

## 결과 파일

결과는 `runs/results-YYYYmmddTHHMMSS.jsonl`(실행 시각 포함)에 한 줄씩 추가된다. 발송이 성공하면 그 즉시 `{"event": "sent", round, gmail_id, uuid_a, uuid_b, tag}`가 먼저 기록되고, 이어서 라운드 결과가 기록된다. 발송 후 단계(폴링, 조회, G5)에서 예외가 나면 `{"event": "round_error", round, gmail_id, error}`를 남기고 추가 발송을 막기 위해 실행을 중단한다(종료 코드 1). `runs/`는 루트 `.gitignore`의 `runs/` 규칙으로 제외된다. 해석은 `docs/spikes/SPIKE-004-gmail-header-preservation.md`의 "결과" 항목에 직접 적는다.

## 테스트 후 정리

1. Google 계정의 보안 페이지(https://myaccount.google.com/permissions)에서 이 앱의 접근 권한을 제거한다.
2. `secrets/gmail-token.json`을 삭제한다.
3. 더 쓰지 않으면 Google Cloud Console에서 OAuth 클라이언트(또는 프로젝트)를 삭제하고 `secrets/gmail-oauth-client.json`도 지운다.
4. 받은편지함·보낸편지함의 `[orbit-spike]` 테스트 메일을 지운다.
