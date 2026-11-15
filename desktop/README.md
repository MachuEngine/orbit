# Orbit 데스크톱 셸 (macOS)

Orbit 웹 화면을 Electron 창으로 여는 셸이다(ADR-0024). 화면·API·쿠키는 서버 출처를 그대로 쓰고, 셸은 창 상주, 메뉴 막대 아이콘, 전역 단축키, 시스템 알림, 독 배지만 더한다. 이 문서의 범위는 13b이며 로그인 핸드오프(13c)와 배포는 아직 없다.

## 실행

Node 22.19 이상이 필요하다. Electron 45.3.2를 정확한 버전으로 고정했다(`package-lock.json`).

```bash
npm --prefix desktop ci
```

개발 서버와 함께 확인하는 순서는 다음과 같다.

1. 백엔드와 웹 개발 서버를 띄운다(`backend/dev.env` 설정으로 백엔드 8000, `npm --prefix frontend run dev`로 5173). Vite가 `/api`를 백엔드로 넘기므로 앱 창도 같은 출처로 API를 쓴다.
2. `npm --prefix desktop run dev`로 앱을 띄운다. 빌드(esbuild)한 뒤 `electron .`을 실행한다.
3. 앱 창에서 dev 로그인으로 들어간다. Google 로그인은 13c 전까지 앱 창에서 쓸 수 없다. 로그인 버튼을 누르면 시스템 브라우저가 열리지만 세션은 앱 창으로 넘어오지 않는다.
4. 확인할 것
   - 창을 닫아도 메뉴 막대에 아이콘이 남고, 아이콘 메뉴의 "Orbit 열기"로 다시 연다. Cmd+Q나 "종료"만 앱을 끝낸다.
   - Option+Space를 누르면 창이 앞으로 나오고 음성 입력이 켜진다. 다시 누르면 끄고 전사한다.
   - 알림 패널에서 "데스크톱 알림" 안내가 보이고, 새 알림이 오면 시스템 알림이 뜨며 독 아이콘에 읽지 않은 수가 보인다.
   - 서명하지 않은 개발 실행에서는 macOS가 알림을 막을 수 있다(ADR-0024 사실 확인). 알림이 안 보이면 시스템 설정의 알림 항목에서 Electron을 확인한다.

## 명령

| 명령                                        | 내용                                                                          |
| ------------------------------------------- | ----------------------------------------------------------------------------- |
| `npm run dev`                               | 빌드 후 앱 실행                                                               |
| `npm run build`                             | `dist/main.cjs`, `dist/preload.cjs` 생성                                      |
| `npm run lint`, `format:check`, `typecheck` | ESLint, Prettier, `tsc --noEmit`                                              |
| `npm test`                                  | 단위 테스트(vitest). 이동·권한 판정, 알림 중복 제거, IPC 인자 검증, 설정 파싱 |
| `npm run test:smoke`                        | 빌드 후 Electron을 실제로 띄우는 스모크 테스트(Playwright `_electron`)        |

스모크 테스트는 `127.0.0.1`의 임의 포트에 테스트 서버를 띄우고 임시 userData와 `ORBIT_DESKTOP_SHORTCUT=off`로 실행한다. 개발 서버(5173, 8000)와 실제 앱 설정, 전역 단축키를 건드리지 않는다. 단, 창이 잠깐 화면에 보인다.

## 환경 변수

| 이름                     | 기본값                  | 설명                                                                                                          |
| ------------------------ | ----------------------- | ------------------------------------------------------------------------------------------------------------- |
| `ORBIT_DESKTOP_URL`      | `http://localhost:5173` | 서버 주소. http/https, 출처만(경로·쿼리·사용자 정보 불가). 잘못되면 시작하지 않는다                           |
| `ORBIT_DESKTOP_SHORTCUT` | `Alt+Space`             | 전역 단축키(Electron accelerator). 빈 값이나 `off`면 쓰지 않는다. 등록에 실패하면 로그만 남기고 계속 실행한다 |

기본 단축키는 `desktop/src/main/config.ts`의 `DEFAULT_SHORTCUT` 한곳에 있다. 앱 안에서 바꾸는 설정 화면은 아직 없다.

## 보안 설정 요약

- `BrowserWindow`: `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`, `webSecurity: true`, `backgroundThrottling: false`(숨겨진 창에서도 30초 알림 조회 유지). 세션은 `persist:orbit` 파티션.
- 창 이동: `will-navigate`·`will-redirect`는 서버 출처가 아니면 막고, `accounts.google.com`(https, 기본 포트, 사용자 정보 없음)만 시스템 브라우저로 넘긴다. 새 창(`setWindowOpenHandler`)은 항상 거부하고 Google 주소만 시스템 브라우저로 연다. 하위 프레임 이동과 `<webview>`도 막는다. 판정은 `src/main/navigation.ts`의 순수 함수이며 `localhost:5173.evil.com`, 사용자 정보 URL, 다른 포트, `file:`, `javascript:` 우회를 테스트한다.
- 권한: 서버 출처의 `media`(오디오만, 비디오가 섞이거나 종류를 모르면 거부)와 `notifications`만 허용한다. 나머지는 요청·확인 핸들러 모두 거부한다. 오디오 요청 때 macOS `getMediaAccessStatus('microphone')`를 보고, 결정 전이면 `askForMediaAccess`를 호출하고, 거부·제한이면 거부한다.
- preload: `window.orbitDesktop`에 `notify`, `setBadge`, `onToggleVoice`, `info`만 노출한다. `ipcRenderer`, Node API, 임의 채널은 없다. IPC 채널은 고정 이름이고, main이 발신자(앱 창 최상위 프레임, 서버 출처)와 인자(타입, 길이 상한, 정수 범위, 모르는 키)를 검증한다.
- 알림: 같은 ID는 한 번만 띄운다(최근 500개 기억). 알림 내용은 로그에 남기지 않는다.
- 단일 인스턴스: 두 번째 실행은 기존 창을 연다.

## 남은 일

13c(로그인·연결 핸드오프, ADR-0024 §4·§5·§7)

- 시스템 브라우저 + loopback 로그인 핸드오프, 계정 연결 티켓, 백엔드 API와 마이그레이션.
- 로그아웃 때 `persist:orbit`의 쿠키·캐시 삭제.
- 마이크가 거부됐을 때 시스템 설정을 여는 안내(지금은 문구 안내만 한다).

배포(ADR-0024 보류)

- `Info.plist`: 개발 실행에 쓰는 Electron 기본 앱에는 `NSMicrophoneUsageDescription`이 이미 있다(확인함). 패키징할 때는 이 키(`NSMicrophoneUsageDescription`)에 Orbit 용도의 설명을 직접 넣어야 한다. 마이크를 쓰는 서명 앱은 hardened runtime에서 `com.apple.security.device.audio-input` entitlement도 필요하다.
- Electron fuse: `RunAsNode`, `EnableNodeOptionsEnvironmentVariable`, `EnableNodeCliInspectArguments`를 끈다(ADR-0024 §3). 개발 실행에는 적용하지 않았다.
- Apple Developer ID 서명·공증, 설치 파일, 자동 업데이트. 서명하지 않으면 알림이 실패할 수 있다.
- 앱 아이콘(`.icns`)과 앱 이름. 지금은 Electron 기본 아이콘이고 메뉴 막대 아이콘만 `docs/brand/logo/mark-gate.svg`에서 만든 템플릿 이미지다(`scripts/make-tray-icon.mjs`로 다시 만든다).
