# Orbit

메일·캘린더·문서를 다루는 대화형 개인 AI 비서다. 사용자가 자연어로 물으면 Agent가 Gmail·Google Calendar·Google Drive를 읽고 답하며, 일정 생성·변경·취소와 메일 발송 같은 외부 쓰기는 사용자가 승인한 뒤에만 실행한다.

이 저장소는 기능 수보다 **평가 방법론**에 무게를 둔다. 모델·Agent 패턴·RAG 구성을 고를 때 합성 평가셋, 봉인 test 분할, cluster bootstrap 신뢰구간으로 측정하고, 측정하지 않은 효과는 개선이라고 쓰지 않는다.

## 현재 상태

- 웹 앱, macOS 데스크톱 셸(Electron), 음성 입력(STT)까지 구현했다.
- 평가 v1을 마쳤고, 측정 결과로 v1 Agent 구성을 확정했다([ADR-0042](docs/adr/ADR-0042-v1-agent-architecture.md)).
- 지금은 개인용 운영 단계다. GCP VM 한 대에 Docker Compose로 띄우고 Tailscale로만 접근하는 구성을 준비하고 있다([ADR-0022](docs/adr/ADR-0022-gcp-vm-deploy.md), `실험 중`). 공개 배포 계획은 없다.

## 주요 기능

| 기능 | 내용 |
| --- | --- |
| 대화형 조회 | 일정·메일·문서에 대한 질문에 답하고 근거가 된 항목을 함께 보여 준다 |
| 메일 검색(RAG) | 메일을 색인해 형태소 키워드 검색과 벡터 검색을 RRF로 합친다. Agent가 필요할 때 검색 도구를 직접 부른다 |
| 승인 후 쓰기 | 일정 생성·변경·취소, 메일 발송은 승인 화면을 거친다. 승인한 인자의 해시와 실행 인자가 같을 때만 서버가 실행한다 |
| 알림 | 일정 시작 전 알림과, 대화로 요청한 시각의 리마인더를 보낸다. 데스크톱에서는 시스템 알림과 독 배지로 보인다 |
| 음성 입력 | 마이크 입력을 전사해 질문으로 보낸다. 데스크톱은 전역 단축키로 켠다 |

## 구조

```text
웹 / 데스크톱 ──HTTP·SSE──> FastAPI ──> 작업 큐(Procrastinate, PostgreSQL)
                                         │
                                       워커: Agent(LangGraph, ReAct)
                                         │
                         Tool Executor (핸들 변환·승인 확인·인자 해시 검증)
                          │                              │
                Connector 계약                         LLM 인터페이스
                ├─ Sim Provider (평가·테스트)          (OpenAI, OpenRouter)
                └─ Google Adapter
                          │
              PostgreSQL + pgvector (workspace별 RLS 격리)
```

- 메일·캘린더·문서 제공자는 Connector 계약 뒤에 둔다. Sim Provider와 Google Adapter가 같은 계약 테스트를 통과한다.
- 모든 사용자 데이터는 `workspace_id`로 나누고, 앱 필터와 PostgreSQL RLS를 함께 건다. 벡터 검색도 같은 SQL 안에서 필터한다.
- 모델은 제공자 ID를 직접 다루지 않고 작업 단위 핸들로 대상을 가리킨다.
- 메일·문서 본문은 신뢰하지 않는 데이터로 다룬다. 쓰기 권한은 프롬프트가 아니라 서버 코드에서 강제한다.

자세한 경계와 결정은 [아키텍처 개요](docs/architecture.md)와 [ADR 목록](docs/adr/)에 있다.

## 기술 스택

- 백엔드: Python, uv, FastAPI, SQLAlchemy 2, psycopg 3, Alembic, LangGraph, Procrastinate
- DB: PostgreSQL 17 + pgvector
- 프론트엔드: React, Vite, TypeScript, zod
- 데스크톱: Electron(macOS)
- 모델: `openai/gpt-6-luna`(Agent), `text-embedding-3-small`(메일 임베딩)

## 평가

평가셋은 모두 합성 데이터다. 가상 사용자의 메일·일정·문서를 코드로 만들고, LLM은 본문 문장과 검수 보조에만 썼다. 정답은 코드가 구조에서 계산한다.

- 종단 과제 218개(dev 88, 봉인 test 130), 검색 질의 133개. 사람이 검수해 결함 과제를 뺐다.
- 성공은 코드 구조 검사(최종 상태·승인·도구 호출)와 LLM 판정(답 본문)을 합쳐 매긴다. 판정기는 사람 판정 62건 중 61건과 일치했다.
- 비교는 같은 과제·반복끼리 짝을 맞춘 차이와 cluster bootstrap 95% CI로 본다. CI가 0을 포함하면 차이를 확인하지 못한 것으로 적는다.

확정한 v1 구성(luna, ReAct, 메일 색인 켬, Hybrid 검색)의 성공률은 dev 0.723(88과제 × 3회), 봉인 test 0.708(130과제 × 1회)이다. 메일 색인을 켜면 dev 성공률이 +0.155 [+0.072, +0.244] 올랐다. 다른 Agent 패턴, 모델, reranker, 추론 강도 비교는 대부분 기준과 차이를 확인하지 못했다.

수치와 조건은 [평가 결과 대장](docs/eval/README.md)과 [평가 결과 v1](docs/eval/results-v1.md)이 정본이다. 가드레일 측정은 [ADR-0033](docs/adr/ADR-0033-safety-guardrail-ablation.md)과 [가드레일 정리](docs/guardrails.md)에 있다.

## 로컬 실행

필요한 것: Docker, [uv](https://docs.astral.sh/uv/), Node 22.19 이상.

```bash
docker compose -f backend/compose.yml up -d --wait
```

```bash
scripts/dev-run.sh migrate
```

아래 세 명령은 각각 다른 터미널에서 띄운다. 두 번째 인자 `sim`을 주면 Google 계정 대신 평가용 가상 사용자 데이터로 동작한다. 어느 모드든 `secrets/.env`가 있어야 한다.

```bash
scripts/dev-run.sh api sim
```

```bash
scripts/dev-run.sh worker sim
```

```bash
scripts/dev-run.sh web
```

브라우저에서 `http://localhost:5173`을 연다. 환경 변수는 [`.env.example`](.env.example)을 참고해 `secrets/.env`에 채운다. 이 파일은 커밋하지 않는다. 데스크톱 셸 실행은 [desktop/README.md](desktop/README.md)에 있다.

## 검사

```bash
scripts/check.sh
```

백엔드 린트·타입·테스트, 프론트엔드·데스크톱 검사, 하네스 검사를 함께 돌린다. 비밀정보 탐지에 [gitleaks](https://github.com/gitleaks/gitleaks)를 쓴다. 로컬에 없으면 경고만 하고 건너뛰며, CI에서는 실패로 처리한다.

## 개발 방식

이 저장소는 Claude Code 코딩 에이전트와 함께 개발했다. 메인 세션이 설계·ADR·작업 명세를 맡고, 구현과 검토는 별도 서브에이전트가 나눠 맡는다. 규칙은 [`CLAUDE.md`](CLAUDE.md)와 `.claude/`에 있다. 이 코딩 하네스는 Orbit 런타임의 Agent·가드레일과 별개다.

세부 설계 원본은 비공개로 관리하며 이 저장소에 포함하지 않는다.
