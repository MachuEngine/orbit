#!/usr/bin/env bash
# Orbit 로컬·CI 공통 품질 검사 진입점. 프로젝트 소유 파일이다.
#
# 사용법: scripts/check.sh
#   CI=true 이면 로컬에서 건너뛸 수 있는 검사(도구 미설치)도 실패로 처리한다.
#
# 하네스 공통 검사(scripts/harness-check.sh)를 먼저 실행하고, 프로젝트 검사를 이어서 실행한다.
# 스택이 정해지면 check_backend / check_frontend에 실제 명령을 채운다.
# 디렉터리가 생겼는데 검사 명령이 비어 있으면 실패한다. 검사 없이 통과하는 상태를 막기 위해서다.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FAILED=0

section() { printf '\n== %s ==\n' "$1"; }
ok()      { printf '  [통과] %s\n' "$1"; }
fail()    { printf '  [실패] %s\n' "$1"; FAILED=1; }

bash scripts/harness-check.sh || FAILED=1

# ── 백엔드 ────────────────────────────────────────────
check_backend() {
  section "백엔드"
  if [ ! -d backend ]; then
    ok "backend/ 없음, 건너뛴다"
    return
  fi
  if ! command -v uv >/dev/null 2>&1; then
    fail "uv가 설치되어 있지 않다 (https://docs.astral.sh/uv/)"
    return
  fi
  if [ -z "${ORBIT_TEST_DATABASE_URL:-}" ] && [ "${CI:-}" != "true" ]; then
    echo "  [안내] ORBIT_TEST_DATABASE_URL이 없어 통합 테스트는 건너뛴다."
    echo "         docker compose -f backend/compose.yml up -d --wait 후"
    echo "         set -a; source backend/dev.env; set +a 로 환경을 맞추면 함께 실행된다."
  fi
  run_backend() {
    local label="$1"; shift
    if uv run --directory backend "$@"; then ok "$label"; else fail "$label"; fi
  }
  run_backend "ruff check" ruff check .
  run_backend "ruff format --check" ruff format --check .
  run_backend "mypy" mypy
  run_backend "pytest" pytest
}

# ── 프론트엔드 ────────────────────────────────────────
check_frontend() {
  section "프론트엔드"
  if [ ! -d frontend ]; then
    ok "frontend/ 없음, 건너뛴다"
    return
  fi
  if ! command -v npm >/dev/null 2>&1; then
    fail "npm이 설치되어 있지 않다 (Node.js 22.19 이상)"
    return
  fi
  # package-lock.json 기준으로 정확히 설치한다. node_modules가 최신이면 건너뛴다.
  if [ ! -d frontend/node_modules ] || [ frontend/package-lock.json -nt frontend/node_modules/.package-lock.json ]; then
    if ! npm ci --prefix frontend; then
      fail "npm ci"
      return
    fi
  fi
  run_frontend() {
    local label="$1"; shift
    if npm --prefix frontend run "$@"; then ok "$label"; else fail "$label"; fi
  }
  run_frontend "eslint" lint
  run_frontend "prettier --check" format:check
  run_frontend "tsc --noEmit" typecheck
  if npm --prefix frontend test -- --run; then ok "vitest"; else fail "vitest"; fi
}

check_backend
check_frontend

echo
if [ "$FAILED" -ne 0 ]; then
  echo "검사 실패"
  exit 1
fi
echo "모든 검사 통과"
