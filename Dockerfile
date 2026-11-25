# Orbit 운영 이미지 (ADR-0022). API, 작업 워커(ORBIT_EMBEDDED_WORKER=true), 빌드한 프론트가 한 프로세스에서 돈다.

# ── 1단계: 프론트 빌드 ───────────────────────────────
FROM node:22-slim AS web
# 프론트 소스가 ../docs/brand(토큰·로고)를 가져오므로 저장소 배치를 /web 아래에 그대로 둔다.
WORKDIR /web/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY docs/brand /web/docs/brand
COPY frontend/ ./
RUN npm run build

# ── 2단계: 백엔드 설치 ───────────────────────────────
FROM python:3.12-slim-bookworm AS backend
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# 프로젝트는 편집 가능 설치라 src/가 /app/backend/src에 그대로 남고, 시작 점검이 같은 위치의
# migrations/에서 코드의 최신 리비전을 읽는다(orbit.api.startup).
COPY backend/src ./src
COPY backend/migrations ./migrations
RUN uv sync --frozen --no-dev

# ── 3단계: 실행 이미지 ───────────────────────────────
FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin orbit
WORKDIR /app/backend
COPY --from=backend /app/backend /app/backend
COPY --from=web /web/frontend/dist /app/static
ENV PATH="/app/backend/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    ORBIT_ENV=prod \
    ORBIT_DEPLOYED=1 \
    ORBIT_STATIC_DIR=/app/static
USER orbit
EXPOSE 8000
# TLS는 앞단 프록시(Tailscale)가 끝낸다. 프록시 헤더는 FORWARDED_ALLOW_IPS의 주소에서 온 것만 믿는다(ADR-0022 §4):
# 기본 127.0.0.1이고 Compose가 프록시 주소를 값으로 준다. `*`를 기본으로 두지 않는다.
# --timeout-graceful-shutdown은 열린 SSE 때문에 lifespan 종료가 막히지 않게 하는 상한이다.
# orbit.runflow.embedded의 UVICORN_GRACEFUL_SHUTDOWN_SECONDS와 같은 값이다(ADR-0022 §8).
CMD ["sh", "-c", "exec uvicorn orbit.api.app:create_app --factory --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-127.0.0.1}\" --timeout-graceful-shutdown 5"]
