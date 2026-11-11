# 구현 계획 v0.1

> 작성일: 2026-11-11. 스파이크 5개와 ADR-0001~0005 채택 이후 본 구현 순서를 정한다. 단계가 끝날 때마다 갱신한다.

## 1. 설계 재점검 결과

### 유지하는 방향
- 웹 우선, 공통 백엔드, Connector 계약 뒤의 Sim/Google 교체 구조 (ADR-0001)
- Run은 LangGraph로 실행하고 중복 실행 방지는 Orbit 테이블이 담당 (ADR-0003)
- 앱 필터 + RLS 이중 격리, 트랜잭션 범위 설정 (ADR-0004)
- Procrastinate, 도메인 변경과 같은 트랜잭션에서 작업 등록 (ADR-0005)
- 메일 발송은 초안 생성 → 초안 발송 (SPIKE-005)

### 새로 발견한 문제
| # | 문제 | 영향 | 처리 |
| --- | --- | --- | --- |
| 1 | LangGraph 체크포인트 테이블(`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`)에 `workspace_id`가 없고, 그래프 상태를 JSON으로 저장한다 | 메일 내용이 그래프 상태에 들어가면 RLS 밖에 사용자 데이터가 쌓인다. 연결 해제 시 삭제 전파(D7-4)에서도 빠진다 | 그래프 상태에는 핸들·ID만 두고 본문은 넣지 않는다. 체크포인트는 `run` 삭제 시 함께 지운다. ADR-0006으로 확정 |
| 2 | Procrastinate `args`도 JSON으로 RLS 밖에 저장된다 | 작업 인자에 본문·주소를 넣으면 같은 문제 | 작업 인자는 ID만 허용. 코드 리뷰 관점에 추가 |
| 3 | Python 3.12에 `uuid7`이 없다 (D4-3) | 표준 라이브러리로 UUIDv7을 만들 수 없다 | 3.12 + 외부 라이브러리 vs 3.14 중 선택. 골격 단계 ADR |
| 4 | Connector 계약이 SPIKE-005의 초안 발송 방식을 반영하지 않았다 | Sim이 초안 의미(발송 후 404, 재발송 거절)를 흉내 내지 않으면 대조 로직을 테스트할 수 없다 | 메일 계약에 초안 생성·발송·조회를 넣고 Sim에도 구현 |
| 5 | API 프레임워크, 프론트엔드, Orbit 자체 로그인 방식, 배포 환경이 미정 | 배포용 관리형 DB가 pgvector 0.8 이상(`iterative_scan`)을 지원해야 한다 (ADR-0004) | 프레임워크는 골격 단계, 프론트·로그인은 웹 단계, 배포는 운영 단계 ADR |
| 6 | 설계 v0.2 문서가 저장소에 없다 | 세부 설계가 참조하는 v0.2 절·단계 번호와의 일치를 확인할 수 없다 | 개발자 확인 필요 |
| 7 | 리뷰어 훅이 테스트 명령(pytest 등)을 허용하지 않고, 작업 폴더가 하위 폴더면 `scripts/check.sh` 실행이 막힌다 (2회 재현) | 백엔드 단계부터 리뷰어가 테스트를 돌릴 수 없다 | 하네스 템플릿 v0.3.0에서 수정 |

## 2. 단계별 구현

각 단계는 하나 이상의 PR로 끝내고, `scripts/check.sh`와 CI 통과를 완료 조건에 포함한다.

### 0단계. 준비 (완료, 2026-11-11, PR #3)
1. 하네스 템플릿 v0.3.0: 리뷰어 훅이 저장소 루트 기준으로 `scripts/check.sh`를 실행하고 `uv run pytest`를 허용하도록 수정 → Orbit에 `copier update`
2. 백엔드 개발 스킬 작성 (프로젝트 구조, 테스트 작성 규칙, RLS·실행 기록 규칙 체크리스트)
3. `main` 브랜치 보호: CI 통과 필수
4. ADR-0006: 그래프 상태·작업 인자에 사용자 본문 금지, 체크포인트 삭제 전파

### 1단계. 백엔드 골격 (완료, 2026-11-11, PR #4)
1. `backend/` uv 프로젝트, lint·타입 검사·테스트 도구 결정 후 `scripts/check.sh`와 CI(PostgreSQL 서비스 포함)에 연결
2. 설정, 고정 시계(Clock) 주입, UUIDv7 생성기
3. 마이그레이션 도구, 소유자·애플리케이션 DB 역할 분리, RLS 정책
4. 트랜잭션 범위 워크스페이스 설정 헬퍼 (ADR-0004 규칙 1~3)
5. RLS 통합 테스트: 스파이크 R2·R4·R6을 제품 테스트로 이식

완료 조건: 다른 워크스페이스 데이터 노출 0을 CI가 매번 검증한다.

결과: 로컬 PostgreSQL(pgvector 0.8.0-pg17)에서 `scripts/check.sh`가 통과했고, RLS 통합 테스트(설정 없음·교차 접근·풀 재사용·동기 스레드/비동기 태스크 동시성·역할 확인)와 마이그레이션 upgrade→downgrade→upgrade가 모두 실행됐다. GitHub Actions(PostgreSQL 서비스)에서도 28건이 skip 없이 통과했다.

### 2단계. Connector 계약과 Sim Provider v0 (완료, 2026-11-11)
1. `MailConnector`·`CalendarConnector` 계약 (메일은 초안 생성·발송·조회 포함)
2. Sim Provider: JSON 스냅샷 → 에피소드별 복제, 상태 diff, FaultPlan, Gmail 검색 부분집합
3. 계약 테스트 묶음 (나중에 Google Adapter도 같은 묶음을 통과해야 한다)

완료 조건: Sim이 계약 테스트를 모두 통과하고, 같은 시드·스냅샷이면 결과가 같다.

결과: 계약(ADR-0008)과 Sim 공통부·캘린더·메일을 병렬로 구현했다(2단계-A~D). 전체 276건 통과·skip 0. 계약 테스트는 Sim 내부 상태에 접근하지 않고, 실제 Google에서 깨질 가정(부분 문자열 검색, 다른 계정 미배달, 스레드의 초안 제외, 색인 지연)은 Sim 단위 테스트로 분리했다. 상태 diff는 Sim이 기록한 초안 생애로 발송을 판정한다. 평가 러너·4단계는 `build_sim_connectors` 팩토리로만 커넥터를 만든다(공유 FaultPlan 봉인 강제).

6단계로 넘길 확인 사항: 실제 Gmail·Calendar의 검색 의미(단어 단위, `to:`의 cc 포함), 생성 직후 검색 색인 지연, `threads.get`의 초안 포함 여부, 취소된 일정 수정·재취소 응답, 계약 테스트 시드를 테스트 계정에 맞추는 방법(캘린더 ID는 Google이 정함).

### 3단계. 일정 조회 경로와 평가 기반
1. 내부 LLM 인터페이스 + OpenRouter 구현 (고정 모델 ID, 토큰·비용 기록)
2. 읽기 전용 일정 질의 경로: 작업 집합 핸들(E1, M3)과 Tool Calling
3. 평가 데이터 v0: 가상 사용자 2명, 질문 60개 (사람 전수 검수 필요)
4. 평가 러너 v0: JSONL 결과, 설정 지문, 반복 실행, 신뢰구간, 예산 상한
5. 모델 파일럿 → 고정 모델 ADR

완료 조건: 같은 설정으로 다시 돌리면 같은 범위의 결과가 나온다. 모델이 고정된다.

진행(2026-11-11, 브랜치 `stage-3/eval-foundation`)
- 3a `orbit.llm`: 내부 LLM 인터페이스, `ScriptedLLM`, `OpenRouterLLM`(제공자 라우팅 고정, 실제 응답 모델 검증, 비용 미상 표시). 완료.
- 3b `orbit.evaldata`: 시나리오 우선 생성기, 세계 모델 `eval/worlds/orbit-v0.toml`(TOML), 데이터셋 `eval/datasets/orbit-eval/v0`(과제 60개, dev 36 / test 24, 근거 공유 묶음 15개). 정답은 구조화된 답(ADR-0010), dev와 test는 근거 일정·메일을 공유하지 않는다. 완료, **사람 검수 대기**.
- 3c `orbit.evalrunner`: 에피소드 실행, 구조화된 답 채점(텍스트 사실은 보조 지표), 결과 JSONL·설정 지문, 반복 우선 실행, 클러스터 bootstrap 신뢰구간·paired 차이·pass^k, 예산 상한, test 봉인. 완료.
- 3d `orbit.assistant`: 읽기 전용 비서 경로(ADR-0011). 작업 집합 핸들, 읽기 도구 4개 + `submit_answer`, 외부 콘텐츠 표지, 프롬프트 `assistant-v0.2`, 온도 0. `ScriptedLLM`으로만 검증했다. 완료.
- 3e 모델 파일럿: OpenRouter API 키(개발자)와 데이터 검수 후 진행.

알려진 한계(v0): test 묶음이 9개라 신뢰구간이 불안정하다. 취소 제외·일정 변경 과제는 캘린더가 최신 상태라 메일 이해보다 도구 사용을 잰다. 텍스트 답 품질은 성공 판정에 들어가지 않는다. 공개 저장소라 test 정답을 누구나 볼 수 있다.

### 4단계. Run 실행과 승인 (쓰기 경로)
1. `run`·`approval`·`execution`·`run_event` 테이블
2. Procrastinate 워커, 멈춘 작업 복구 주기 작업
3. LangGraph interrupt 승인 대기, 승인 조건부 갱신, 실행 기록 선기록 (ADR-0003)
4. SSE 이벤트 중계와 재접속(`Last-Event-ID`)
5. 일정 변경·메일 발송(초안 방식) 쓰기 도구를 Sim에서 실행
6. 통합 테스트: 스파이크 S4·S7 상황에서도 외부 쓰기가 정확히 1번인지

완료 조건: 강제 종료·동시 승인 상황에서도 외부 쓰기 1번, 승인 없는 쓰기 0번.

진행(2026-11-11, 브랜치 `stage-4/write-path`, ADR-0012): 3e(모델 파일럿)보다 먼저 진행했다(개발자 결정).
- 4A `orbit.records`: 마이그레이션 0002(run, run_event, run_message, tool_call, approval, execution; RLS·FORCE·열 단위 권한), 조건부 승인, 실행 선기록·실행 소유권(lease, DB 시계), Procrastinate 같은 트랜잭션 등록, 멈춘 작업 복구, run 삭제 전파.
- 4B `orbit.actions`: 쓰기 동작 4종, 인자 해시, 인자 출처 분류, 실행기와 결과 불명 대조(클라이언트 지정 ID, `if_match`, 초안 발송), 만료·비승인 재진입은 읽기 대조만.
- 4C `orbit.runflow`·`orbit.api`: LangGraph 쓰기 Run 그래프(step 멱등, interrupt 노드 분리), Procrastinate 워커, Run·승인 API와 SSE, 개발용 임시 인증(`ORBIT_ENV=dev`에서만).
- 통합 테스트: 승인 없는 쓰기 0, S4(쓰기 직후 종료 후 재시도)·S7(동시 승인)·결과 불명 대조에서 쓰기 정확히 1번, 체크포인트·작업 인자에 원문 없음.
- 미검증: 실제 LLM, 실제 서버(uvicorn)의 SSE 점진 전송, 실제 교착 재현, 메일 결과 불명의 통합 시나리오(단위 테스트만), Google에서의 동작(6단계).

### 5단계. 웹 수직 슬라이스 (H1)
1. 프론트엔드 스택 ADR, UX/UI 스킬 작성
2. 채팅 화면 → API → 응답 스트리밍
3. 승인 화면 (인자 출처 경고 포함, D6-1)

진행(2026-11-11, 브랜치 `stage-5/web-slice`, ADR-0013): 스택 React + Vite + TypeScript, 세션은 쿠키(개발자 결정). 백엔드 쿠키 세션·CSRF·`/api` 경로(5A), 프론트엔드 데이터·상태 계층(5B), 브랜드 확정(흑연 팔레트, IBM Plex Sans KR, 게이트 로고, 이름 Orbit — `docs/brand/`), 로그인·채팅·승인 화면(5D, 승인 대상의 변경 전 값 표시 포함). 실제 LLM 흐름과 실제 브라우저의 SSE 재접속은 미검증이다.

### 6단계. 실제 Google 연동 (H2)
1. Orbit 로그인 방식 ADR, 사용자·워크스페이스 생성
2. Google OAuth 연결, 토큰 봉투 암호화 (D4-5)
3. Google Calendar Adapter → 계약 테스트 통과
4. Gmail Adapter (초안 발송) → 계약 테스트 통과, 제한 스코프 요구 사항 확인

### 이후 (개요)
- 7단계 RAG(H3): 선택 범위 인덱싱, 검색 방식 비교 실험, 워크스페이스 크기별 벡터 검색 recall 측정
- 8단계 Agent 패턴(H4): Fixed Workflow·ReAct·Plan & Execute 비교, 인젝션 공격 평가
- 9단계 운영(H5): 다중 사용자, 관측, 배포 환경 ADR

## 3. 개발자 결정이 필요한 것
| 시점 | 결정 |
| --- | --- |
| ~~지금~~ | ~~3단계(평가 기반)를 5단계(웹 화면)보다 먼저 하는 순서~~ → **결정(2026-11-11): 평가 기반 먼저** |
| 지금 | 설계 v0.2 문서 제공 여부 |
| 1단계 | Python 3.12 + UUIDv7 라이브러리 vs Python 3.14 |
| 3단계 | OpenRouter 계정·API 키 준비, 평가 데이터 60문항 사람 검수 |
| 5단계 | 프론트엔드 스택 |
| 6단계 | Orbit 로그인 방식 |
