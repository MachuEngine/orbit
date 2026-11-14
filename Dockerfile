# Orbit 데모 이미지 (ADR-0022). Render 무료 Web Service에 Docker로 배포한다.
# API, 작업 워커(ORBIT_EMBEDDED_WORKER=true), 빌드한 프론트가 한 프로세스에서 돈다.

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
    ORBIT_STATIC_DIR=/app/static
USER orbit
EXPOSE 10000
# Render가 주는 $PORT를 쓴다(없으면 10000). TLS는 Render 프록시가 끝낸다.
# FORWARDED_ALLOW_IPS: 기본 *. Render 서비스는 Render 프록시를 거쳐서만 닿는다(근거: docs/deploy/render-neon.md).
CMD ["sh", "-c", "exec uvicorn orbit.api.app:create_app --factory --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-*}\""]
