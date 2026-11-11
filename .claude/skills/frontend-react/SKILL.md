---
name: frontend-react
description: React + Vite + TypeScript 프론트엔드(npm, ESLint, Prettier, Vitest, Testing Library, zod)를 만들거나 고치거나 검토할 때 쓴다. 디렉터리 구조, 같은 출처 쿠키·CSRF fetch 래퍼, zod 응답 검증과 오류 종류, SSE 구독, 토큰만 쓰는 스타일, 한글 IME 가드, 접근성 기본, 테스트 규칙, .gitignore 주의, 완료 체크리스트를 담는다.
---

# React 프론트엔드 개발 규칙

> **프로젝트 보충 규칙**: `.claude/project-skills/frontend-react.md`가 있으면 이 문서보다 먼저 읽는다. 두 문서가 충돌하면 보충 규칙이 우선한다. API 경로, 이벤트 종류, 화면별 규칙, 실제 명령은 보충 규칙에 적는다.
>
> 이 파일은 하네스 템플릿(스킬 팩 `react-frontend`)이 관리한다. 프로젝트에서 직접 고치지 말고, 다른 프로젝트에도 필요한 변경이면 템플릿 저장소에 반영한다.
>
> 화면의 시각 디자인은 `frontend-design` 스킬, 화면 검토는 `design-review` 스킬(스킬 팩 `ui-design`)을 함께 쓴다.

복사해서 쓸 설정·코드 예시는 `reference/`에 있다. 파일 이름 끝의 `.example`을 떼고 `<버전>`, `app_csrf` 같은 자리를 프로젝트 값으로 바꾼다.

| 파일 | 놓을 위치 |
| --- | --- |
| `reference/package.json.example` | `frontend/package.json` (scripts·engines 블록과 의존성 이름) |
| `reference/vite.config.ts.example` | `frontend/vite.config.ts` |
| `reference/eslint.config.js.example` | `frontend/eslint.config.js` |
| `reference/tsconfig.json.example` | `frontend/tsconfig.json` |
| `reference/prettierrc.json.example`, `reference/prettierignore.example` | `frontend/.prettierrc.json`, `frontend/.prettierignore` |
| `reference/test-setup.ts.example` | `frontend/src/test/setup.ts` |
| `reference/api-client.ts.example`, `reference/api-errors.ts.example` | `frontend/src/api/client.ts`, `errors.ts` |
| `reference/subscribe.ts.example` | SSE를 쓰는 기능 폴더 (예: `frontend/src/<기능>/subscribe.ts`) |
| `reference/useComposer.ts.example` | Enter로 전송하는 입력창이 있는 기능 폴더 |
| `reference/check-frontend.sh.example` | `scripts/check.sh`의 `check_frontend` 함수 |
| `reference/ci-frontend.yml.example` | `.github/workflows/ci.yml`의 job에 합친다 |

## 스택
| 영역 | 도구 | 비고 |
| --- | --- | --- |
| UI | React, TypeScript strict | `noUncheckedIndexedAccess`까지 켠다 |
| 빌드 | Vite | 정적 파일로 배포한다 |
| 패키지 | npm | `package-lock.json`을 커밋하고 CI는 `npm ci` |
| 검사 | ESLint(typescript-eslint 타입 검사 규칙), Prettier `--check`, `tsc --noEmit`, Vitest + Testing Library | 순서: eslint → prettier → tsc → vitest |
| 런타임 검증 | zod | 서버 응답과 이벤트는 모두 스키마로 검증한다 |

버전은 이 문서에 적지 않는다. `package.json`과 `package-lock.json`이 기준이다. Node 버전은 `engines`와 CI에 같은 값으로 고정한다.

## 디렉터리 구조
```text
frontend/
  package.json, package-lock.json
  vite.config.ts, tsconfig.json, eslint.config.js, .prettierrc.json, .prettierignore
  index.html
  src/
    main.tsx, App.tsx
    api/          client.ts(fetch 래퍼), errors.ts(오류 종류), schemas.ts(zod), endpoints.ts(경로)
    <기능>/       화면·훅·로직·테스트를 기능 단위로 모은다 (예: chat/, session/, settings/)
    ui/           여러 기능이 같이 쓰는 컴포넌트
    styles/       토큰 불러오기, 기본 스타일, 글꼴
    test/setup.ts
```
- 기능 폴더 안에서 로직(`logic.ts`, `reducer.ts`)과 화면(`*.tsx`)을 나눈다. 로직은 DOM 없이 테스트한다.
- 컴포넌트가 `fetch`를 직접 부르지 않는다. `api/client.ts`를 거친다.

## 반드시 지킬 규칙

### API 호출: 같은 출처 쿠키 + CSRF 헤더
- 웹과 API를 **같은 출처**로 둔다. 개발 중에는 Vite 프록시로 `/api`를 백엔드에 넘긴다. CORS를 열어 다른 출처에서 쿠키를 보내는 구성을 만들지 않는다.
- 세션은 서버가 내리는 `HttpOnly` 쿠키로 유지한다. 토큰을 `localStorage`·`sessionStorage`나 스크립트가 읽을 수 있는 곳에 두지 않는다.
- 상태를 바꾸는 요청(POST·PUT·PATCH·DELETE)에는 서버가 내린 CSRF 쿠키 값을 헤더(예: `X-CSRF-Token`)로 붙인다(이중 제출). 래퍼가 자동으로 처리하고 호출하는 쪽은 신경 쓰지 않게 한다.
- `fetch`는 `credentials: "same-origin"`으로 부른다.

### 응답 검증과 오류 종류
- 서버 응답 본문은 zod 스키마로 `safeParse`한다. 타입 단언(`as`)으로 서버 응답을 믿지 않는다.
- 오류는 종류로 나눈다: `unauthorized`(401, 로그인 화면으로), `csrf`(403), `not_found`(404), `conflict`(409, 경쟁·만료), `network`(서버에 닿지 못함), `server`(5xx·그 밖의 4xx), `invalid_response`(형태가 다름). 화면은 종류에 따라 다음 행동을 안내한다.
- 서버의 오류 `detail` 문자열은 구분용으로만 쓰고 화면에 그대로 보여 주지 않는다. 화면 문구는 프론트엔드에서 정한다.

### SSE 구독
- `EventSource`는 같은 출처 쿠키로 인증한다. 토큰을 쿼리 문자열에 넣지 않는다.
- 이벤트마다 서버가 단조 증가하는 `id`(seq)를 붙이고, 끊기면 브라우저가 `Last-Event-ID`로 재접속한다. 클라이언트는 **이미 받은 seq 이하를 버린다**. 재접속 때 서버가 재생한 이벤트가 두 번 반영되지 않게 한다.
- 이벤트 데이터도 zod로 검증한다. 모르는 이벤트 종류는 무시하고, 알려진 종류인데 형태가 다르면 오류로 알린다.
- 종료 상태 이벤트를 받으면 연결을 닫는다. `onerror`에서 `readyState`가 CLOSED면 재접속 포기, 아니면 재접속 중으로 구분해 화면에 알린다.
- 구독 함수는 해제 함수를 돌려주고, 컴포넌트가 사라질 때 반드시 부른다.

### 스타일: 토큰만 쓴다
- 색, 글꼴, 글자 크기, 간격, 모서리, 그림자, 모션은 디자인 토큰(CSS 사용자 정의 속성)만 쓴다. 컴포넌트에 hex·`rgb()`·임의 px 글자 크기를 직접 쓰지 않는다.
- 토큰이 없으면 `brand-identity` 스킬로 먼저 만든다. 임시 값으로 화면을 만들지 않는다. 토큰이 정해지기 전에는 데이터·상태 계층부터 만든다.
- 모션은 `prefers-reduced-motion: reduce`에서 꺼지게 토큰에서 처리한다.

### 한글 입력(IME) 가드
- Enter로 전송하는 입력창은 IME 조합 중인 Enter를 무시한다. `compositionstart`/`compositionend`로 조합 상태를 추적하고, 키 이벤트의 `isComposing`이 참이거나 `keyCode === 229`이면 전송하지 않는다(Safari는 조합이 끝난 직후 Enter에 `isComposing=false`, `keyCode=229`를 준다). 이 검사가 없으면 마지막 글자가 두 번 들어가거나 조합 중인 문장이 잘린 채 전송된다.
- Shift+Enter는 줄바꿈이다. 전송 중에는 다시 전송하지 않고, 실패하면 입력을 비우지 않고 재시도할 수 있게 한다(`reference/useComposer.ts.example`).

### 접근성 기본
- 버튼은 `<button>`, 링크는 `<a href>`를 쓴다. `div`에 클릭 처리기를 달지 않는다.
- 입력창마다 보이는 이름표나 `aria-label`이 있다. placeholder로 이름표를 대신하지 않는다. 오류 문구는 `aria-describedby`로 입력창과 연결한다.
- 글자 없는 아이콘 버튼에는 `aria-label`, 장식 아이콘에는 `aria-hidden="true"`를 둔다.
- `outline: none`을 쓰면 `:focus-visible` 대체 스타일을 둔다. 양수 `tabindex`를 쓰지 않는다.
- 모달이 열리면 포커스를 안으로 옮기고 닫히면 연 버튼으로 돌려준다.
- 스트리밍 응답·상태 변화는 `aria-live`로 알리되 문장·단계 단위로 알린다(토큰마다 알리면 낭독이 끊긴다).

### 외부 콘텐츠
- 외부에서 온 HTML을 `dangerouslySetInnerHTML`로 넣어야 하면 허용 목록 기반으로 정제한 값만 넣는다. 원격 이미지·CSS·iframe을 자동으로 불러오지 않는다.
- 외부 링크는 `rel="noopener noreferrer"`로 연다.

## `.gitignore` 주의
저장소 루트 `.gitignore`의 디렉터리 규칙(예: `runs/`, `build/`, `logs/`, `coverage/`)은 **모든 깊이**에 적용된다. 기능 폴더 이름이 이 규칙과 같으면 `frontend/src/runs/` 같은 소스 폴더가 통째로 git에서 빠지고, 로컬에서는 테스트가 통과해도 CI와 다른 사람의 체크아웃에는 파일이 없다.
- 새 소스 폴더를 만들면 `git check-ignore -v frontend/src/<폴더>/<파일>`로 무시되지 않는지 확인한다. 출력이 없어야 정상이다.
- 루트 전용 규칙은 앞에 `/`를 붙여 범위를 좁힌다(`/runs/`). 이미 넓은 규칙이 있으면 `!frontend/src/<폴더>/` 예외를 둔다.
- 커밋 전에 `git status --short`에 새 폴더의 파일이 보이는지 확인한다.

## 테스트
- 로직(리듀서, 상태 계산, 스키마 파싱)은 DOM 없이 Vitest로 테스트한다.
- 화면은 Testing Library로 사용자가 보는 이름(`getByRole`, `getByLabelText`)으로 찾는다. 클래스·`data-testid`는 마지막 수단이다.
- 사용자 입력은 `@testing-library/user-event`로 재현한다. IME 조합 Enter, 전송 중 재전송, 실패 후 재시도를 테스트한다.
- `fetch`와 `EventSource`는 래퍼의 주입 지점(생성자 옵션, 팩토리 인자)으로 바꿔 끼운다. 전역을 덮어쓰지 않는다.
- 테스트 데이터는 합성 값만 쓴다.
- 실패를 숨기려고 테스트를 지우거나 `skip`하지 않는다.

## 명령
저장소 루트에서 실행한다. 프로젝트 보충 규칙에 실제 명령이 있으면 그쪽을 따른다.
- 전체 검사: `scripts/check.sh`
- 개발 서버: `npm --prefix frontend run dev` (백엔드가 프록시 대상 포트에서 돌고 있어야 한다)
- 개별 실행: `npm --prefix frontend run lint`, `npm --prefix frontend run format:check`, `npm --prefix frontend run typecheck`, `npm --prefix frontend test -- --run`
- 의존성 추가: `npm --prefix frontend install <패키지>@<정확한 버전>` 후 `package-lock.json`을 함께 커밋한다. `package.json`에는 범위(`^`) 대신 정확한 버전을 적는다.
- code-reviewer는 Bash 훅 때문에 `npm`을 직접 실행할 수 없다. `scripts/check.sh`로 확인한다.

## 완료 체크리스트
- [ ] API 호출이 모두 `api/client.ts`를 거치고, 응답을 zod로 검증한다
- [ ] 상태를 바꾸는 요청에 CSRF 헤더가 붙고, 토큰을 스크립트가 읽는 저장소에 두지 않는다
- [ ] SSE가 seq 중복을 버리고, 종료·재접속·포기를 구분하고, 해제 함수를 부른다
- [ ] 색·글자 크기·간격이 토큰에서 온다
- [ ] Enter 전송 입력창에 IME 가드와 테스트가 있다
- [ ] 아이콘 버튼 이름, 입력창 이름표, 포커스 표시가 있다
- [ ] 새 소스 폴더가 `git check-ignore`에 걸리지 않는다
- [ ] `scripts/check.sh`가 통과한다
