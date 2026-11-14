# Orbit 보충 규칙: shadcn

> `.claude/skills/shadcn/SKILL.md`(shadcn-ui/ui 공식 스킬)보다 이 문서가 우선한다.

## 쓰는 것
- 컴포넌트 선택 기준, 상태 설계(빈 화면·로딩·오류·비활성), 폼 구조(필드·라벨·설명·검증 표시), 키보드·포커스·ARIA 패턴, 채팅 UI 구성(`rules/chat.md`의 스크롤·점프·활동 표시 개념)을 **참고**한다.
- `rules/composition.md`의 접근성 규칙(대화상자에는 제목, 메뉴 항목은 그룹 안)은 Orbit에도 적용한다.

## 쓰지 않는 것
- shadcn CLI(`npx shadcn@latest ...`) 실행, `components.json` 생성, 컴포넌트 소스 복사, Tailwind·Radix·base-ui·class-variance-authority·`cn()` 도입. Orbit은 순수 CSS + `docs/brand/tokens.css` 토큰만 쓴다(`frontend/src/styles/brand.test.ts`가 강제한다).
- shadcn의 기본 스타일(둥근 모서리, 부드러운 그림자, 프리셋 색). Orbit은 직각·코발트·웜 그레이·잉크 네이비다.
- `design-system.md`의 "DESIGN.md → 새 앱 만들기" 절차. Orbit의 브랜드 기준은 `docs/brand/brand-guide.md`다.
- 의존성 추가나 외부 레지스트리 설치.

## 적용 방법
shadcn의 패턴을 Orbit 마크업·CSS 클래스·토큰으로 옮겨 구현한다. 예: Empty → 빈 화면 레트로 창/큰 제목, Alert → `.note tone-*`, Skeleton·Spinner → `.loading-dots`, FieldGroup → `.label` + `.input`.
