#!/usr/bin/env bash
# Orbit 로컬 개발 실행 도우미. 프로젝트 소유 파일이다.
#
# 사용법: scripts/dev-run.sh <api|worker|web|migrate> [sim|google]
#   api     FastAPI 서버(127.0.0.1:8000)
#   worker  Procrastinate 워커(Run 실행)
#   web     Vite 개발 서버(127.0.0.1:5173, /api 프록시). 브라우저는 http://localhost:5173 으로 연다
#   migrate 개발 DB를 최신 마이그레이션으로 올린다
#   두 번째 인자는 커넥터 모드(기본 google). sim은 평가 데이터의 가상 사용자 메일·캘린더를 쓴다.
#
# 필요한 것:
#   - docker compose -f backend/compose.yml up -d --wait (개발 DB)
#   - backend/dev.env (저장소에 있음, 개발 전용 값)
#   - secrets/.env (개발자 로컬, 커밋하지 않음): ORBIT_GOOGLE_CLIENT_ID/SECRET, ORBIT_GOOGLE_REDIRECT_URI,
#     ORBIT_SESSION_SIGNING_KEY, ORBIT_LOGIN_ALLOWED_EMAILS, ORBIT_DATA_KEY_FILE(절대 경로), ORBIT_OPENAI_API_KEY
#   변수 설명은 .env.example. 이 스크립트는 secrets/.env 내용을 출력하지 않는다.
#
# 모델(ADR-0015): 실사용 기본은 openai/gpt-6.1-terra(low). ORBIT_LLM_MODEL_ID 등을 미리 export하면 그 값을 쓴다.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cmd="${1:-}"
mode="${2:-google}"

load_env() {
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/backend/dev.env"
  if [ -f "$ROOT/secrets/.env" ]; then
    # shellcheck disable=SC1091
    source "$ROOT/secrets/.env"
  else
    echo "secrets/.env가 없다. .env.example을 참고해 만든다." >&2
    exit 2
  fi
  set +a
  export ORBIT_ENV=dev
  export ORBIT_CONNECTOR_MODE="$mode"
  export ORBIT_LLM_PROVIDER="${ORBIT_LLM_PROVIDER:-openai}"
  export ORBIT_LLM_MODEL_ID="${ORBIT_LLM_MODEL_ID:-openai/gpt-6.1-terra}"
  export ORBIT_LLM_REASONING_EFFORT="${ORBIT_LLM_REASONING_EFFORT:-low}"
  export ORBIT_LLM_PRICE_INPUT_PER_MTOK="${ORBIT_LLM_PRICE_INPUT_PER_MTOK:-1}"
  export ORBIT_LLM_PRICE_CACHED_INPUT_PER_MTOK="${ORBIT_LLM_PRICE_CACHED_INPUT_PER_MTOK:-0.05}"
  export ORBIT_LLM_PRICE_OUTPUT_PER_MTOK="${ORBIT_LLM_PRICE_OUTPUT_PER_MTOK:-5}"
  export ORBIT_LLM_PRICE_SOURCE="${ORBIT_LLM_PRICE_SOURCE:-developers.openai.com/api/docs/pricing (2026-11-11)}"
  if [ "$mode" = "sim" ]; then
    export ORBIT_SIM_SNAPSHOT_PATH="${ORBIT_SIM_SNAPSHOT_PATH:-$ROOT/eval/datasets/orbit-eval/v0/world.json}"
    export ORBIT_SIM_ACCOUNT_ID="${ORBIT_SIM_ACCOUNT_ID:-acct-seoyeon}"
  fi
}

case "$cmd" in
  api)
    load_env
    cd "$ROOT/backend"
    exec uv run uvicorn orbit.api.app:create_app --factory --host 127.0.0.1 --port 8000
    ;;
  worker)
    load_env
    cd "$ROOT/backend"
    exec uv run python -m orbit.runflow.worker
    ;;
  web)
    cd "$ROOT/frontend"
    exec npm run dev -- --host 127.0.0.1 --port 5173
    ;;
  migrate)
    set -a
    # shellcheck disable=SC1091
    source "$ROOT/backend/dev.env"
    set +a
    cd "$ROOT/backend"
    exec uv run alembic upgrade head
    ;;
  *)
    sed -n '2,20p' "$0"
    exit 2
    ;;
esac
