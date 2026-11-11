# ADR-0013 — 프론트엔드 스택과 세션 방식

- 상태: 채택 (2026-11-11, 개발자 결정: 스택은 React + Vite + TypeScript, 세션은 쿠키)
- 작성일: 2026-11-11
- 근거: ADR-0001(웹 우선·공통 백엔드), ADR-0012(Run·승인 API, SSE), `docs/implementation-plan.md` 5단계, 세부 설계 D5-2·D6-1 (비공개, `private-docs/`)

## 결정

### 1. 프론트엔드 스택
| 항목 | 선택 | 이유 |
| --- | --- | --- |
| UI | React + TypeScript(strict) | 로그인 뒤에만 쓰는 화면이라 서버 렌더링이 필요 없다. 컴포넌트·접근성·테스트 자료가 가장 많다 |
| 빌드 | Vite, 정적 파일로 배포 | 별도 Node 서버를 운영하지 않는다. 인증·권한은 모두 FastAPI가 맡는다 |
| 패키지 관리 | npm, `package-lock.json` 커밋 | 추가 설치 없이 쓸 수 있다 |
| 검사 | ESLint, Prettier(`--check`), `tsc --noEmit`, Vitest + Testing Library | `scripts/check.sh` 프론트엔드 절과 CI에 연결 |
| 위치 | 저장소 루트 `frontend/` | |

- 개발 환경에서는 Vite 개발 서버의 프록시로 `/api`를 FastAPI에 넘겨 **같은 출처(same origin)**로 쓴다. 운영에서도 웹과 API를 같은 도메인에 둔다.
- 화면 스타일은 `docs/brand/tokens.css`의 토큰만 쓴다. 브랜드가 정해지기 전에는 화면 스타일을 만들지 않고, 데이터·상태 계층부터 만든다(`.claude/skills/frontend-design/reference/orbit.md` 2절).

### 2. 세션은 쿠키
- 로그인하면 서버가 무작위 세션 토큰을 `HttpOnly`, `Secure`(개발 환경의 http는 예외), `SameSite=Lax`, `Path=/` 쿠키로 내린다. DB에는 토큰의 해시만 저장하고, 세션은 워크스페이스(6단계부터 사용자)에 묶이며 만료 시각을 가진다. 로그아웃·연결 해제 시 서버에서 즉시 무효화한다.
- **CSRF**: 상태를 바꾸는 요청(POST·PUT·PATCH·DELETE)은 이중 제출 토큰을 요구한다. 서버가 읽기 가능한 CSRF 쿠키를 내리고, 화면은 같은 값을 `X-CSRF-Token` 헤더로 보낸다. 서버는 두 값이 같고 세션에 묶인 값과 일치할 때만 처리한다. `SameSite=Lax`와 함께 쓴다.
- SSE(`EventSource`)는 같은 출처 쿠키로 인증한다. 토큰을 쿼리 문자열에 넣지 않는다.
- **6단계 전 임시 로그인**: `ORBIT_ENV=dev`에서만 열리는 개발용 로그인 엔드포인트가 설정된 개발 워크스페이스의 세션 쿠키를 발급한다. ADR-0012의 `X-Orbit-Workspace` 헤더 인증은 이 쿠키 세션으로 대체한다. dev가 아니면 앱이 시작되지 않는 규칙은 유지한다.

## 검토한 대안
| 대안 | 채택하지 않은 이유 |
| --- | --- |
| Next.js | 서버 렌더링·라우트 서버의 이점이 이 화면 구성에서는 쓰이지 않고, 운영할 서버가 하나 는다 |
| SvelteKit, SolidJS | 번들이 작지만 컴포넌트·접근성 자료가 React보다 적다 |
| 토큰(JWT) + `Authorization` 헤더 | 토큰을 스크립트가 읽을 수 있는 곳에 두면 XSS 한 번으로 계정 권한이 새고, 즉시 무효화를 위해 갱신 토큰 체계가 필요하다. `EventSource`는 헤더를 붙일 수 없다. 브라우저가 아닌 클라이언트가 생기면 그때 별도로 추가한다 |
| SSE 대신 WebSocket | 단방향 이벤트 중계에는 SSE로 충분하고 재접속(`Last-Event-ID`)을 표준으로 지원한다 |

## 결과
- 데스크톱 앱(후속)은 같은 정적 빌드를 웹뷰로 감싸 같은 쿠키 세션을 쓸 수 있다.
- 6단계 로그인 ADR은 "어떻게 사용자를 확인하는가"만 정하면 되고, 세션 유지 방식은 이 ADR을 따른다.
