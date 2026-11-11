# Orbit 개발 규칙

@.claude/harness/WORKFLOW.md

> 위 줄은 하네스 공통 규칙(역할 분담, 작업 순서, 하네스 개선 절차)을 불러온다. 지우지 않는다.
> 이 파일에는 Orbit에만 해당하는 규칙을 쓴다. 공통 규칙과 충돌하면 이 파일이 우선한다.
> 하네스 템플릿: https://github.com/MachuEngine/claude-harness-template (버전은 `.copier-answers.yml`)

## 제품 목표
- 웹을 먼저 개발하는 대화형 개인 AI 비서. 메일·캘린더·문서를 다룬다.
- 메일·캘린더·문서 제공자는 교체 가능한 Connector 계약 뒤에 둔다. Sim Provider와 Google Adapter가 같은 계약을 구현한다.
- 데스크톱 앱은 후속 확장 대상이며 같은 백엔드 API를 재사용한다.
- 포트폴리오의 핵심 산출물은 패턴 개수가 아니라 평가 방법론의 엄밀성이다. 재현성과 측정 가능성을 기능 수보다 우선한다.

## 먼저 읽을 문서
- 경계와 구성: `docs/architecture.md`, `docs/adr/`
- 구현 순서와 현재 단계: `docs/implementation-plan.md`
- 스킬은 하네스 스킬 팩의 범용 스킬(`.claude/skills/<스킬>/SKILL.md`, 템플릿 소유)과 Orbit 보충 규칙(`.claude/project-skills/<스킬>.md`, 충돌 시 우선)을 함께 읽는다.
- 백엔드 코드를 쓰거나 검토할 때: `.claude/skills/backend-python/SKILL.md` + `.claude/project-skills/backend-python.md`
- 프론트엔드 코드를 쓰거나 검토할 때: `.claude/skills/frontend-react/SKILL.md`
- 화면을 만들 때: `.claude/skills/frontend-design/SKILL.md`(Anthropic 원본) + `.claude/project-skills/frontend-design.md`
- 브랜드(색·서체·로고·토큰)를 정하거나 바꿀 때: `.claude/skills/brand-identity/SKILL.md` + `.claude/project-skills/brand-identity.md` (브랜드는 `docs/brand/brand-guide.md`에서 채택 상태)
- 화면 변경을 검토할 때: `.claude/skills/design-review/SKILL.md` + `.claude/project-skills/design-review.md`
- 세부 결정과 근거: `private-docs/Orbit — 세부 설계 초안 v0.1.md` (D1~D10)
- 하네스 운영 원칙: `private-docs/Orbit_코딩에이전트_하네스_설계_v0.1.md`

## 결정 상태
- 세부 설계의 `후보`는 스파이크 전이므로 확정된 것처럼 다루지 않는다. 스파이크 후 ADR로 확정한다.

## 프로젝트 금지·주의
- 실제 메일·문서 원문을 코드·로그·테스트 픽스처에 남기지 않는다. 테스트와 평가에는 합성 데이터만 쓴다.
- 사용자 승인 없이 외부 발송·삭제·쓰기를 실행하는 경로를 만들지 않는다. 권한은 프롬프트가 아니라 서버 코드에서 강제한다.
- 모든 사용자 데이터 접근에 `workspace_id` 필터를 건다. 다른 사용자 데이터 노출은 출시 차단 조건이다.
- RAG·Agent·모델·DB의 개선 효과를 측정 없이 사실처럼 쓰지 않는다.
- 이 저장소는 공개 저장소다. 커밋 전에 개인정보와 비밀정보가 없는지 확인한다.
- 코딩 하네스(`CLAUDE.md`, `.claude/`)와 Orbit 런타임의 Agent·가드레일을 섞지 않는다.

## 프로젝트 검토 관점
code-reviewer가 공통 검토 관점보다 먼저 확인할 항목이다.
1. **데이터 격리**: 사용자 데이터 조회·검색·삭제에 `workspace_id` 필터가 빠지지 않았는가. 벡터 검색도 같은 SQL 안에서 필터하는가 (D4-1, D4-4)
2. **승인과 쓰기 실행**: 외부 쓰기가 승인된 인자 해시와 일치할 때만 실행되는가. `execution` 행을 외부 호출보다 먼저 기록하는가. 결과가 불명확할 때 재호출하지 않고 대조하는가 (D5-4, D5-5)
3. **외부 콘텐츠**: 메일·문서 본문을 신뢰하는 코드가 없는가. 쓰기 도구 인자의 출처를 추적하는가. 임의 URL 조회나 자동 링크 로딩 같은 유출 경로가 생기지 않았는가 (D6-1)
4. **Connector 경계**: 제공자 API를 Connector 계약 밖에서 직접 호출하지 않는가. Sim과 Google Adapter가 같은 계약 테스트를 통과하는가 (D1-1)
5. **모델 ID와 핸들**: 모델이 제공자 ID를 직접 다루지 않고 작업 집합 핸들(E1, M3)로만 대상을 가리키는가 (D8-1)
6. **재현성**: 실험 코드가 고정 시계, 고정 모델 ID, 설정 지문을 쓰는가 (D1-5, D3-2, D9-3)
7. **RLS 밖 저장소**: LangGraph 그래프 상태와 Procrastinate 작업 인자에 메일·문서 본문, 주소, 이름, 프롬프트 원문이 들어가지 않는가. ID와 핸들만 있는가 (ADR-0006)
8. **화면 변경**: diff에 프론트엔드 파일(css, html, ts/tsx, js/jsx 등)이 있으면 `.claude/skills/design-review/SKILL.md`와 `.claude/project-skills/design-review.md`를 Read로 읽고 그 점검표와 보고 형식을 함께 적용한다. 프론트엔드 코드는 `.claude/skills/frontend-react/SKILL.md`의 완료 체크리스트도 확인한다
9. **백엔드 변경**: diff에 `backend/` 파일이 있으면 `.claude/skills/backend-python/SKILL.md`와 `.claude/project-skills/backend-python.md`의 완료 체크리스트를 함께 확인한다
