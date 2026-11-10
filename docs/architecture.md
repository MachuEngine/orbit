# Orbit 아키텍처 개요

> 상태: 초안 (2026-11-10). 아직 구현 코드가 없다. 아래 구성은 세부 설계 초안 v0.1(비공개, `private-docs/`)의 결정을 옮긴 것이며, 코드가 생기면 실제 구현에 맞춰 갱신한다.
>
> 상태 표기는 세부 설계를 따른다: **방향 확정** / **후보**(스파이크 전) / **실험** / **미결정**.

## 1. 전체 구성

```text
[웹 클라이언트]   [데스크톱 클라이언트(후속)]
        │                 │
        └──── HTTP API + SSE (Run 이벤트 구독) ────┐
                                                  ▼
                                         [API 서버]
                                   Run 생성 → 큐 등록, run_event 중계
                                                  │
                                         [작업 큐 (PostgreSQL 기반)]
                                                  │
                                         [워커]
                               Orchestrator / Agent 패턴 (LangGraph)
                                                  │
                                         [Tool Executor]
                         핸들 → 제공자 ID 변환, 승인 확인, 인자 해시 검증
                                                  │
                         ┌────────────────────────┴───────────────────┐
                 [MailConnector / CalendarConnector 계약]      [LLM 인터페이스]
                   ├─ Sim Provider (인메모리, 실험·테스트)          └─ OpenRouter
                   └─ Google Adapter (실제 연동)
                                                  │
                                         [PostgreSQL + pgvector]
                          workspace 격리(앱 필터 + RLS), run/approval/execution,
                          source_item/chunk, run_event, audit_log
```

## 2. 모듈 경계

| 경계 | 규칙 | 근거 | 상태 |
| --- | --- | --- | --- |
| 클라이언트 ↔ 백엔드 | 웹과 데스크톱은 같은 HTTP API와 SSE를 쓴다. UI는 LangGraph 내부 상태에 의존하지 않고 Orbit 테이블만 읽는다. | D5-2, D5-3 | 후보 |
| 제공자 ↔ Orchestrator | 제공자는 `MailConnector`·`CalendarConnector` 계약으로만 접근한다. Sim과 Google Adapter에 같은 계약 테스트를 돌린다. | D1-1 | 후보 |
| 모델 ↔ 실제 ID | 모델은 작업 집합의 짧은 핸들(E1, M3)로만 대상을 가리킨다. 제공자 ID 변환은 서버가 한다. | D8-1 | 후보 |
| 쓰기 실행 | 승인된 인자 해시와 일치할 때만 실행한다. `execution` 행을 먼저 기록하고 외부 호출은 나중에 한다. 결과가 불명확하면 재호출하지 않고 대조한다. | D5-4, D5-5 | 방향 확정 |
| 데이터 격리 | 모든 사용자 데이터 행에 `workspace_id`를 두고 앱 필터와 RLS로 이중 격리한다. | D4-4 | 후보 |
| 외부 콘텐츠 | 메일·문서 본문은 공격 입력으로 취급한다. 인자 출처 추적, 유출 경로 차단, 최소 도구 노출을 코드로 강제한다. | D6-1 | 방향 확정 |
| LLM 호출 | 내부 LLM 인터페이스 하나로만 호출한다. 토큰·비용 기록과 모델 버전 고정을 이곳에서 강제한다. | D9-2, D9-3 | 후보 / 방향 확정 |

## 3. 기술 선택 현황

| 영역 | 선택 | 상태 |
| --- | --- | --- |
| 백엔드 언어 | Python (LangGraph, SQLAlchemy, Procrastinate 전제) | 후보, ADR 필요 |
| 프론트엔드 | 미정 | 미결정 |
| DB | PostgreSQL + pgvector | 후보 (D4-1) |
| 작업 큐 | Procrastinate | 후보 (D7-5) |
| Agent 실행 | LangGraph (그래프 내부 상태만 담당) | 후보 (D5-3) |
| LLM 게이트웨이 | OpenRouter + 고정 모델 1개 | 방향 확정, 모델은 미결정 (D9-1, D9-4) |
| 한국어 검색 | 앱에서 형태소 분석 후 저장 | 후보 (D4-2) |

## 4. 먼저 검증할 가정 (스파이크)

아래 가정이 틀리면 해당 결정을 다시 연다. 결과는 ADR로 남긴다.

1. LangGraph interrupt + checkpointer로 승인 대기 → 워커 재시작 → 재개가 되는가 (D5-3)
2. PostgreSQL RLS를 SQLAlchemy 연결 풀과 함께 쓸 수 있는가 (D4-4)
3. Procrastinate 작업 등록을 도메인 변경과 같은 트랜잭션에 묶을 수 있는가 (D7-5)
4. Gmail 발송 시 앱이 넣은 헤더가 보존되어 결과 대조에 쓸 수 있는가 (D5-4)

## 5. 코딩 하네스와 런타임의 분리

개발용 코딩 에이전트 하네스(`CLAUDE.md`, `.claude/`, `scripts/check.sh`, CI)와 Orbit 런타임의 Agent·가드레일은 별개다. 하네스 평가는 테스트 통과·결함·개발 시간으로, 서비스 평가는 RAG·Agent 성공률·보안 지표로 측정하며 두 실험을 섞지 않는다.
