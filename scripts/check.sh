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
  # TODO(스택 확정 후): lint / type check / test 명령을 여기에 추가하고 아래 fail을 지운다.
  fail "backend/가 있지만 검사 명령이 정의되지 않았다. scripts/check.sh를 갱신한다"
}

# ── 프론트엔드 ────────────────────────────────────────
check_frontend() {
  section "프론트엔드"
  if [ ! -d frontend ]; then
    ok "frontend/ 없음, 건너뛴다"
    return
  fi
  # TODO(스택 확정 후): lint / type check / test 명령을 여기에 추가하고 아래 fail을 지운다.
  fail "frontend/가 있지만 검사 명령이 정의되지 않았다. scripts/check.sh를 갱신한다"
}

check_backend
check_frontend

echo
if [ "$FAILED" -ne 0 ]; then
  echo "검사 실패"
  exit 1
fi
echo "모든 검사 통과"
