# ADR-0016 — Google 로그인, 계정 연결, 토큰 봉투 암호화

- 상태: 채택 (2026-11-11, 개발자 결정: 로그인은 Google 로그인. 세부 구현은 개발자가 단계 진행을 위임한 범위에서 메인 세션이 결정)
- 작성일: 2026-11-11
- 근거: ADR-0004(RLS), ADR-0008(Connector 계약), ADR-0013(쿠키 세션), SPIKE-004·005(Gmail), 세부 설계 D4·D4-5·D7-4 (비공개, `private-docs/`)

## 결정

### 1. 로그인 = Google OIDC (authorization code + PKCE)
- `GET /api/auth/google/start` → Google 인가 화면(범위 `openid email profile`만), `state`·`nonce`·PKCE `code_verifier`는 서버가 만들어 짧은 수명의 서명 쿠키(HttpOnly, SameSite=Lax)에 둔다.
- `GET /api/auth/google/callback` → `state` 대조, 코드 교환, **ID 토큰 검증**(Google JWKS 서명, `iss`, `aud`=클라이언트 ID, `exp`, `nonce`, `email_verified`). 실패는 로그인 화면으로 돌려보내고 원인 종류만 남긴다.
- 사용자 식별은 Google `sub`(이메일이 아니다). 처음 로그인하면 `app_user`와 개인 워크스페이스(`workspace.type=personal`), `membership`(owner)을 한 트랜잭션에서 만든다.
- 로그인에 성공하면 ADR-0013의 쿠키 세션을 발급한다. 세션은 사용자와 워크스페이스에 묶인다. 세션 고정을 막기 위해 로그인 때마다 새 토큰을 발급한다.
- 개발용 로그인(`/api/dev/login`)은 `ORBIT_ENV=dev`에서만 유지한다. 개발 환경이 아니어도 앱이 시작되도록 "dev가 아니면 시작 거부" 규칙을 이 단계에서 해제하되, 그 조건은 **Google 로그인 설정(클라이언트 ID·비밀·리디렉션 주소)이 모두 있을 때**다.
- **가입 허용 목록**: 출시 전까지 로그인 가능한 이메일을 설정의 허용 목록으로 제한한다(Gmail 제한 범위의 테스트 모드와 맞춘다).

### 2. 계정 연결 = 별도 OAuth 동의 (점진 권한)
- 로그인 계정과 데이터 연결 계정을 분리한다. `connection` 테이블(workspace_id, user_id, provider=`google`, account_email, account_sub, scopes, status, token 암호문, created_at, revoked_at)을 둔다. RLS·FORCE.
- `GET /api/connections/google/start` → Gmail·Calendar 범위를 `include_granted_scopes=true`, `access_type=offline`, `prompt=consent`로 요청. 콜백에서 받은 범위가 요청 범위를 모두 포함할 때만 `active`.
- 범위(v0): Calendar `https://www.googleapis.com/auth/calendar.events`(+ 목록 조회용 `calendar.readonly`), Gmail `gmail.readonly`·`gmail.compose`(초안 생성·발송, SPIKE-005). 정확한 범위 목록은 구현에서 최소화하고 결정 기록에 남긴다.
- 연결 해제: Google 토큰 철회 호출 → `connection.status=revoked`, 토큰 암호문 삭제 → 연결에 딸린 데이터 삭제 전파(D7-4, 지금은 동기화 데이터가 없어 대상이 없다). 철회 호출이 실패해도 로컬 해제와 토큰 삭제는 진행하고, 화면에서 Google 계정 권한 페이지에서 직접 해제하도록 안내한다(서버가 토큰을 보관한 채 재시도하는 것보다 보관하지 않는 쪽을 택한다).
- Google은 refresh_token 하나를 철회하면 같은 사용자·클라이언트의 권한 전체를 거둘 수 있다고 본다. 그래서 같은 계정의 활성 연결이 있을 때 새로 받은 토큰은 철회하지 않고 버린다.

### 3. 토큰 봉투 암호화 (D4-5)
- 리프레시 토큰(와 캐시된 액세스 토큰)은 연결마다 무작위 데이터 키(DEK)로 AES-256-GCM 암호화하고, DEK는 키 암호화 키(KEK)로 감싸 함께 저장한다. 연관 데이터(AAD)에 `connection_id`와 `workspace_id`를 넣어 행 사이 바꿔치기를 막는다.
- KEK: 개발은 `secrets/` 아래 파일 키(`ORBIT_DATA_KEY_FILE`), 운영은 KMS(9단계). KEK 식별자를 암호문에 기록해 회전을 가능하게 한다.
- 토큰 원문은 로그·예외·작업 인자·그래프 상태에 남기지 않는다(ADR-0006). 복호화는 커넥터를 만드는 순간에만 한다.

### 4. Google Adapter
- `GoogleCalendarConnector`, `GmailConnector`가 ADR-0008 계약을 구현한다. HTTP는 httpx, 액세스 토큰은 주입한 토큰 공급자에서 받는다(만료 시 리프레시, 리프레시 실패·철회는 `PermissionRevoked`).
- 오류 대응: 429·`rateLimitExceeded`는 `RateLimited`, 5xx·연결 오류는 읽기는 `TransientError`, **쓰기는 요청이 전송된 뒤의 타임아웃·연결 끊김을 `ResultUnknown`**으로(재호출 금지, ADR-0003), 전송 전 실패는 `TransientError`.
- 계약 테스트는 HTTP 모의로 항상 돌리고, 실제 Google 테스트 계정 실행은 명시적 플래그(`ORBIT_CONTRACT_GOOGLE=1`)가 있을 때만 한다. 2단계에서 넘긴 확인 사항(검색 의미, 색인 지연, `threads.get`의 초안 포함, 취소 일정 재수정)을 이 실행에서 확인해 기록한다.
- 워커·평가 러너의 `ConnectorResolver`에 Google 구현을 추가한다. 평가는 계속 Sim으로만 한다.

### 5. 작업 분할
| 작업 | 내용 | 의존 |
| --- | --- | --- |
| 6A | `app_user`·`membership`, Google OIDC 로그인, 세션의 사용자 연결, 허용 목록, 시작 규칙 변경, 로그인 화면 버튼 | 없음 |
| 6C | Google Calendar Adapter + 모의 계약 테스트 | 없음 |
| 6D | Gmail Adapter(초안 방식) + 모의 계약 테스트 | 없음 |
| 6B | `connection`, 봉투 암호화, 연결·해제 흐름, 토큰 공급자, `ConnectorResolver`, 연결 화면 | 6A |
| 6E | 실제 테스트 계정 계약 테스트 실행과 확인 사항 기록 | 6B·6C·6D, 개발자 Google 로그인 |

## 검토한 대안
| 대안 | 채택하지 않은 이유 |
| --- | --- |
| 로그인과 연결을 한 번의 동의로 | 처음부터 메일 권한을 요구하면 로그인 장벽이 크고, 연결 해제와 로그인 계정의 수명이 섞인다 |
| 이메일을 사용자 키로 | 이메일은 바뀔 수 있다. `sub`가 고정 식별자다 |
| 토큰을 DB 평문 + 디스크 암호화만 | 덤프·쿼리 경로 유출을 막지 못한다(D4-5) |
| 연결마다 Secrets Manager 항목 | 시크릿 수에 비례한 비용과 지연(D4-5) |

## 결과
- 개발자가 할 일: Google Cloud 콘솔에서 웹 애플리케이션 OAuth 클라이언트 생성(리디렉션 주소 2개: 로그인·연결 콜백), 동의 화면 범위 등록, 테스트 사용자 등록, 클라이언트 ID·비밀을 `secrets/.env`에 저장.
- 공개 출시 전에는 Google 앱 검증과 제한 범위 보안 평가가 필요하다(9단계).
