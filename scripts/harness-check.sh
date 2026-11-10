#!/usr/bin/env bash
# 하네스 공통 검사. 하네스 템플릿이 관리하는 파일이다. 프로젝트에서 직접 고치지 않는다.
#
# scripts/check.sh가 이 스크립트를 먼저 실행한다. 단독 실행도 가능하다.
#   CI=true 이면 로컬에서 건너뛸 수 있는 검사(도구 미설치)도 실패로 처리한다.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

IS_CI="${CI:-false}"
FAILED=0

section() { printf '\n== %s ==\n' "$1"; }
ok()      { printf '  [통과] %s\n' "$1"; }
warn()    { printf '  [경고] %s\n' "$1"; }
fail()    { printf '  [실패] %s\n' "$1"; FAILED=1; }

# ── 1. 비밀정보 탐지 ──────────────────────────────────
check_secrets() {
  section "비밀정보 탐지 (gitleaks)"
  if ! command -v gitleaks >/dev/null 2>&1; then
    if [ "$IS_CI" = "true" ]; then
      fail "gitleaks가 설치되어 있지 않다"
    else
      warn "gitleaks가 없어 건너뛴다. 설치: brew install gitleaks"
    fi
    return
  fi

  # 커밋된 이력 검사
  if git rev-parse --verify HEAD >/dev/null 2>&1; then
    if gitleaks detect --source . --redact --no-banner; then
      ok "커밋 이력에서 비밀정보를 찾지 못했다"
    else
      fail "커밋 이력에서 비밀정보 의심 항목을 찾았다"
    fi
  else
    ok "커밋이 아직 없어 이력 검사를 건너뛴다"
  fi

  # 스테이징된 변경 검사 (로컬에서 커밋 전에 실행할 때 의미가 있다)
  if [ "$IS_CI" != "true" ]; then
    if gitleaks protect --staged --redact --no-banner; then
      ok "스테이징된 변경에서 비밀정보를 찾지 못했다"
    else
      fail "스테이징된 변경에서 비밀정보 의심 항목을 찾았다"
    fi
  fi
}

# ── 2. 하네스 설정 검사 ───────────────────────────────
check_harness_config() {
  section "하네스 설정"
  local f tracked

  for f in .claude/settings.json; do
    if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$f" 2>/dev/null; then
      ok "$f JSON 형식"
    else
      fail "$f JSON 형식 오류"
    fi
  done

  if python3 -m unittest discover -s tests/harness -q >/dev/null 2>&1; then
    ok "하네스 훅 테스트 (tests/harness)"
  else
    fail "하네스 훅 테스트 실패: python3 -m unittest discover -s tests/harness -v 로 확인한다"
  fi

  for f in scripts/*.sh; do
    if bash -n "$f"; then
      ok "$f 문법"
    else
      fail "$f 문법 오류"
    fi
  done

  # 실제 .env 파일이 추적되고 있지 않은지 확인
  tracked="$(git ls-files -- '.env' '.env.*' '**/.env' '**/.env.*' | grep -v '\.env\.example$' || true)"
  if [ -n "$tracked" ]; then
    fail "git이 추적 중인 .env 파일이 있다: $tracked"
  else
    ok ".env 파일이 추적되지 않는다"
  fi

  # 비공개 문서가 추적되고 있지 않은지 확인 (git add -f로 강제 추가한 경우를 잡는다)
  tracked="$(git ls-files -- 'private-docs' || true)"
  if [ -n "$tracked" ]; then
    fail "git이 추적 중인 비공개 문서가 있다: $tracked"
  else
    ok "private-docs/가 추적되지 않는다"
  fi

  # copier update 뒤에 남은 병합 충돌 표시와 .rej 파일
  local conflicts
  conflicts="$(git grep --untracked -lE '^(<<<<<<<|>>>>>>>)( |$)' 2>/dev/null || true)"
  conflicts="$conflicts$(git ls-files --others --exclude-standard -- '*.rej')"
  if [ -n "$conflicts" ]; then
    fail "병합 충돌이 정리되지 않았다: $(echo "$conflicts" | tr '\n' ' ')"
  else
    ok "병합 충돌 표시와 .rej 파일이 없다"
  fi
}

# ── 3. 템플릿 소유 파일 변경 알림 ─────────────────────
# 템플릿 소유 파일이 커밋 이후 바뀌었는데 copier update로 바뀐 것이 아니면(.copier-answers.yml이 그대로면)
# 프로젝트에서 직접 고친 것이다. 템플릿 저장소 반영을 잊지 않도록 경고한다. 검사를 실패시키지는 않는다.
check_template_drift() {
  section "하네스 템플릿 동기화"
  local list=".claude/harness/owned-files.txt" paths=() line changed

  if [ ! -f "$list" ]; then
    warn "$list 이 없어 건너뛴다"
    return
  fi
  if ! git rev-parse --verify HEAD >/dev/null 2>&1; then
    ok "커밋이 아직 없어 건너뛴다"
    return
  fi

  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    paths+=("$line")
  done < "$list"

  changed="$(git status --porcelain -- "${paths[@]}")"
  if [ -z "$changed" ]; then
    ok "템플릿 소유 파일이 마지막 커밋과 같다"
  elif [ -n "$(git status --porcelain -- .copier-answers.yml)" ]; then
    ok "copier update로 바뀐 템플릿 소유 파일이다"
  else
    warn "템플릿 소유 파일을 프로젝트에서 직접 고쳤다. 다른 프로젝트에도 필요한 변경이면 하네스 템플릿 저장소에도 반영한다 (.claude/harness/WORKFLOW.md '하네스 개선 절차'):"
    printf '%s\n' "$changed" | sed 's/^/           /'
  fi
}

check_secrets
check_harness_config
check_template_drift

if [ "$FAILED" -ne 0 ]; then
  echo
  echo "하네스 공통 검사 실패"
  exit 1
fi
