# ADR-0007 — 백엔드 골격 기술 선택

- 상태: 채택 (2026-11-11, 개발자가 단계별 진행을 위임한 범위에서 메인 세션이 결정. 이의가 있으면 1단계 PR 리뷰 때 바꾼다)
- 작성일: 2026-11-11
- 근거: ADR-0002(Python), ADR-0004(RLS), ADR-0005(Procrastinate), `docs/implementation-plan.md` §1 문제 3·5

## 배경

1단계(백엔드 골격)를 시작하려면 Python 버전, UUIDv7 생성 방법, API 프레임워크, 마이그레이션 도구, 검사 도구를 정해야 한다. 이 선택은 이후 모든 단계의 코드 구조와 CI에 영향을 준다.

## 결정

| 항목 | 선택 | 이유 |
| --- | --- | --- |
| Python | 3.12 (`>=3.12,<3.13`) | 스파이크 5개가 모두 3.12에서 검증됐다. 3.14는 일부 의존성 휠 지원을 아직 확인하지 않았다 |
| UUIDv7 | 표준 라이브러리만으로 RFC 9562 UUIDv7을 만드는 작은 내부 함수 + 단위 테스트 | 3.12에 `uuid.uuid7`이 없다. 20줄 안팎이라 외부 의존성을 늘릴 이유가 없다. 3.14로 올리면 표준 함수로 교체한다 |
| API 프레임워크 | FastAPI + SSE 응답 (Starlette `StreamingResponse` 또는 검증된 SSE 라이브러리) | 비동기 SQLAlchemy·psycopg와 맞고, 요청 스키마 검증(Pydantic)이 기본 제공된다. SSE 재접속(`Last-Event-ID`)은 4단계에서 구현 |
| DB 접근 | SQLAlchemy 2.x (동기·비동기), 드라이버 `postgresql+psycopg` (psycopg 3) | SPIKE-002·003에서 RLS·Procrastinate와 함께 검증했다 |
| 마이그레이션 | Alembic, 소유자 역할로 실행 | SQLAlchemy 표준. RLS 정책·FORCE·역할 권한을 SQL로 함께 관리한다 |
| 린트·포맷 | ruff (`ruff check`, `ruff format --check`) | 하나의 도구로 린트와 포맷 검사를 한다 |
| 타입 검사 | mypy (`strict`에 가깝게, 단계적으로 강화) | SQLAlchemy·Pydantic 플러그인 생태계가 넓다 |
| 테스트 | pytest, 통합 테스트는 docker compose의 PostgreSQL(pgvector 0.8.0-pg17) | 스파이크와 같은 이미지로 RLS·iterative scan 동작을 그대로 검증한다 |
| CI DB | GitHub Actions `services:`로 같은 이미지 | 로컬과 같은 버전으로 통합 테스트를 매번 실행한다 |

## 검토한 대안

| 대안 | 채택하지 않은 이유 |
| --- | --- |
| Python 3.14 | 표준 `uuid7`을 쓸 수 있지만 LangGraph·psycopg·Procrastinate 조합을 3.14에서 검증하지 않았다 |
| UUIDv7 외부 라이브러리 (`uuid6`, `uuid-utils`) | 기능이 작아 직접 구현과 테스트가 더 단순하다 |
| Litestar, Starlette 단독 | 문서·예제·생태계가 FastAPI보다 작다. 1인 개발에서 학습 비용이 더 크다 |
| pyright | 빠르지만 SQLAlchemy 플러그인 지원은 mypy가 더 성숙하다. 속도 문제가 생기면 재검토 |
| 테스트용 SQLite·모킹 DB | RLS·pgvector·Procrastinate를 검증할 수 없다 |

## 결과

- `scripts/check.sh`의 백엔드 절에 ruff·mypy·pytest를 연결하고, CI에 PostgreSQL 서비스를 추가한다.
- 리뷰어 훅(하네스 템플릿 v0.3.0)이 `uv run --directory backend pytest|ruff|mypy`를 허용해야 한다.

## 재검토 트리거

- 주요 의존성이 Python 3.13 이상을 공식 지원하고 3.12 지원을 줄일 때
- mypy 실행 시간이 CI 병목이 될 때
