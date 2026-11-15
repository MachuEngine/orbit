# ADR-0024 — 데스크톱 앱: Electron 셸 (macOS 먼저)

- 상태: 채택(셸 선택), 세부는 `실험 중`. 배포(서명·공증·자동 업데이트)는 보류.
- 작성일: 2026-11-15
- 근거: ADR-0001(웹 우선, 같은 API·SSE), ADR-0013(같은 출처 쿠키 세션·CSRF), ADR-0016(Google 로그인·연결), ADR-0021(알림), D6-1(외부 콘텐츠)
- 사실 확인(2026-11-15, 공식 문서): Google OAuth는 임베디드 웹뷰에서 로그인을 금지하고(`disallowed_useragent`) 데스크톱 앱은 시스템 브라우저와 로컬(loopback) 리디렉션, PKCE를 쓰라고 한다. Electron은 `globalShortcut`, `Notification`, `systemPreferences.askForMediaAccess`(Info.plist `NSMicrophoneUsageDescription` 필요)를 제공하고, macOS에서 서명하지 않은 앱은 알림이 실패한다.

## 문제
웹은 탭이 닫히면 알림을 못 받고(ADR-0021 보류), 다른 앱을 쓰다가 바로 말을 걸 방법이 없다. 데스크톱 앱은 후속 확장 대상이었다(ADR-0001). 음성 입력(ADR-0025)도 전역 단축키와 상주 실행이 있어야 쓸모가 커진다.

## 결정

### 1. 셸과 대상
- Electron. 사용 사례가 많고 OS가 달라도 화면·마이크 동작이 같다(Chromium 포함). Tauri는 앱이 작지만 OS별 웹뷰 차이를 따로 검증해야 해서 택하지 않았다.
- 첫 대상은 macOS. Windows는 후속.
- 저장소 최상위 `desktop/`(TypeScript, main·preload 프로세스). 웹 화면 코드는 `frontend/`를 그대로 쓴다.

### 2. 화면은 서버 출처를 그대로 연다
- 앱 창은 Orbit 서버 주소(설정값, 개발 중에는 로컬 서버)를 `loadURL`로 연다. 화면·API·쿠키가 같은 출처라 ADR-0013의 쿠키 세션·CSRF를 바꾸지 않는다.
- 정적 빌드를 앱에 넣고 원격 API를 부르는 방식은 출처가 달라져 CORS·`SameSite=None` 쿠키가 필요하므로 택하지 않는다. 대신 오프라인 사용은 지원하지 않는다.
- 앱 전용 세션 저장소(`persist:orbit` 파티션)를 쓰고 로그아웃 때 쿠키·캐시를 지운다.

### 3. 셸 보안
- `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`. preload는 `contextBridge`로 필요한 기능만 `window.orbitDesktop`에 노출한다(알림 표시, 독 배지, 단축키 이벤트, 로그인·연결 시작, 앱 정보). 웹 코드는 이 객체가 없으면 웹 동작으로 돌아간다.
- 창 이동은 서버 출처로 제한한다. 다른 출처로 가려는 이동과 새 창은 막고, Google 로그인·동의 주소만 시스템 브라우저로 연다. 화면에는 외부 링크를 그리지 않는다는 원칙(D6-1)을 유지한다.
- 권한 요청은 서버 출처의 `media`(오디오만)와 `notifications`만 허용하고 나머지는 거부한다.
- Electron fuse로 `RunAsNode`·Node 옵션 환경 변수·inspect 인자를 끈다. 원격 출처에 Node 기능을 주지 않는다.

### 4. 로그인: 시스템 브라우저 + loopback 핸드오프
Google 로그인은 앱 창에서 열 수 없다. 다음 순서로 앱 창에 세션을 넘긴다.
1. 앱이 임의의 `code_verifier`를 만들고 `127.0.0.1`의 임시 포트에 일회용 수신기를 연다.
2. 시스템 브라우저로 `GET /api/auth/desktop/start?code_challenge=…&port=…`를 연다. 서버는 값을 검증해 기록하고 기존 Google 로그인으로 보낸다.
3. Google 콜백에서 로그인이 끝나면, 서버는 데스크톱 요청이었으면 브라우저에 세션을 만들지 않고 일회용 핸드오프 코드(60초, 1회, 해시로 저장, `code_challenge`에 묶임)를 만들어 `http://127.0.0.1:<port>/callback?code=…`로 보낸다. 리디렉션 호스트는 `127.0.0.1`만 허용한다.
4. 앱이 코드를 받으면 앱 창 안에서 `POST /api/auth/desktop/exchange {code, code_verifier}`를 호출하고, 서버가 검증 후 앱 창의 쿠키 저장소에 세션 쿠키를 설정한다.
- 커스텀 URL 스킴(`orbit://`)은 다른 앱이 가로챌 수 있어 쓰지 않는다. 허용 목록·세션 TTL은 웹과 같다.

### 5. 계정 연결(Gmail·Calendar·Drive 동의): 앱이 완료한다
- 위협(13c 검토에서 발견): 세션 없는 브라우저에서 티켓만으로 연결을 끝내면, 공격자가 자기 티켓 URL을 피해자에게 열게 해 피해자의 Google 계정을 공격자 워크스페이스에 연결할 수 있다(연결 CSRF). 웹 흐름은 세션 쿠키와 `state` 쿠키가 같은 브라우저에 있어야 해서 이 문제가 없다.
- 결정: 브라우저는 동의만 받고, 연결 저장은 앱이 자기 세션으로 완료 코드를 제출할 때만 한다(로그인 핸드오프와 같은 PKCE + loopback 구조).
  1. 앱 main이 `code_verifier`·`code_challenge`·앱 `state`를 만들고 `127.0.0.1` 임시 포트에 수신기를 연다. renderer는 main에게서 `code_challenge`·포트·앱 `state`만 받아(verifier는 main에만) `POST /api/connections/google/desktop-ticket`(세션·CSRF)에 함께 보내고 일회용 티켓 URL을 받는다.
  2. 시스템 브라우저에서 `desktop-begin` 안내 화면을 열고, 사용자가 "계속"을 누르면(POST, 링크 미리보기·프리페치가 티켓을 소진하지 않게) 티켓을 소비하고 서명 쿠키에 워크스페이스·사용자·`code_challenge`·포트·앱 `state`를 묶어 Google로 보낸다.
  3. 콜백은 토큰을 교환·저장하지 않는다. Google 인가 코드를 봉투 암호화해 대기 행(5분, 1회, 완료 코드 해시와 `code_challenge`에 묶임)에 두고 `http://127.0.0.1:<port>/callback?code=…&state=…`로 보낸다. 브라우저에는 "Orbit 앱으로 돌아가세요"를 보인다.
  4. 앱 main이 `state`를 확인하고 앱 세션으로 `POST /api/connections/google/desktop-complete {code, code_verifier}`(세션·CSRF)를 호출한다. 서버는 세션의 워크스페이스·사용자가 대기 행과 같고 verifier가 맞을 때만 Google 인가 코드를 교환하고 기존 저장 규칙(계정 일치, 점진 권한)으로 연결을 저장한다.
- 효과: 공격자가 피해자에게 URL을 열게 해도 완료 코드는 피해자 PC의 `127.0.0.1`로 가고 공격자 세션에서는 제출할 수 없다. 완료되지 않으면 토큰이 발급되지 않고 대기 행은 만료 후 지워진다.
- 앱은 완료 결과를 화면에 알리고 연결 목록을 다시 불러온다.
- 한계: 같은 PC의 악성 로컬 프로세스가 loopback 포트를 선점하면 로그인 코드를 가로챌 수 있다(§4도 같다). PKCE verifier가 앱 main에만 있어 교환은 막히지만, 공격자가 시작한 로그인 URL을 사용자가 열고 로그인하면 그 프로세스가 자기 verifier로 교환할 수 있다. 로컬 프로세스가 이미 악성인 경우는 위협 모델 밖으로 둔다.

### 6. 데스크톱 기능
- 상주: 창을 닫아도 메뉴 막대 아이콘으로 남고, 알림 조회(30초)를 계속한다. 종료는 메뉴에서.
- 알림: 웹의 브라우저 알림 대신 Electron `Notification`으로 띄운다(창이 닫혀 있어도). 같은 알림 ID는 한 번만. 독 배지에 읽지 않은 수.
- 전역 단축키(기본 `Option+Space`, 설정에서 변경): 창을 앞으로 가져오고 음성 입력을 켠다. 다시 누르면 끈다(ADR-0025 토글 방식).
- 마이크 권한: 처음 음성 입력 때 `askForMediaAccess('microphone')`. 거부되면 시스템 설정으로 안내한다.

### 7. 백엔드 변경
- 새 API: `/api/auth/desktop/start`, `/api/auth/desktop/exchange`, `/api/connections/google/desktop-ticket`, `/api/connections/google/desktop-begin`. 핸드오프 코드·연결 티켓은 해시·ID·시각만 저장한다(ADR-0006). 세션 조회(`auth_session`, 0004)와 같은 방식으로 RLS(FORCE) 테이블에 두고 `orbit_app`은 INSERT만 하며, 세션이 없는 소비 시점에는 해시로 찾아 조건부 `UPDATE … RETURNING` 하는 `SECURITY DEFINER` 함수만 쓴다. 만료 행은 15분 주기 작업이 지운다(마이그레이션 0012).
- 데스크톱 경로의 상태(로그인의 PKCE 값·loopback 포트, 연결의 워크스페이스·사용자)는 기존 서명 쿠키(`orbit_login`·`orbit_connect`)에 함께 묶는다. 이 값이 없으면 웹 흐름과 같다.
- 기존 웹 흐름은 바꾸지 않는다.

### 8. 검증
- main 프로세스 로직(이동 제한, 권한 처리, loopback 수신기, 단축키)은 단위 테스트.
- Playwright의 Electron 지원으로 스모크 테스트: 앱 실행 → 서버 화면 로드 → `window.orbitDesktop` 노출 확인 → 외부 출처 이동 차단.
- 백엔드 핸드오프·티켓 API는 통합 테스트(만료, 재사용, 다른 `code_verifier`, 다른 워크스페이스, `127.0.0.1` 외 호스트 거부).
- 실제 Google 로그인은 개발자 계정으로 수동 확인.

## 보류
- 배포: Apple Developer ID 서명·공증, 자동 업데이트, 설치 파일 배포. 다른 사람에게 배포할 때 정한다(서명 없이는 알림이 실패하므로 그 전까지는 개발 실행으로만 쓴다).
- Windows 지원, 오프라인 모드, 로컬 색인(ADR-0001 대안).

## 검토한 대안
| 대안 | 채택하지 않은 이유 |
| --- | --- |
| Tauri v2 | 앱이 작지만 OS별 웹뷰 차이 검증이 필요하고 대형 사용 사례가 적다 |
| 정적 빌드 내장 + 원격 API | 출처가 달라져 쿠키·CSRF 구조(ADR-0013)를 바꿔야 한다 |
| 앱 창에서 Google 로그인 | Google 정책상 금지(`disallowed_useragent`) |
| 커스텀 URL 스킴으로 세션 전달 | 다른 앱이 스킴을 가로챌 수 있다. loopback + PKCE가 Google 권장과 같은 방식이다 |
