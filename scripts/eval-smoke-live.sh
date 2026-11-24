#!/usr/bin/env bash
# 실제 모델 스모크(비용 발생)를 로컬에서 실행한다. .github/workflows/eval-smoke-live.yml과 같은 명령이다.
#
# 사용법: scripts/eval-smoke-live.sh <runflow_v0|assistant_v0> <model_id> [max_cost_usd=0.2]
#   - 키는 secrets/.env를 source만 한다(값을 출력하지 않는다). 단가(ORBIT_LLM_PRICE_*)와
#     ORBIT_LLM_PROVIDER_ORDER도 그 파일이나 환경에 있어야 한다.
#   - runflow_v0는 평가 DB(orbit_eval)가 필요하다: docs/pilot/README.md "평가 DB 준비".
#   - 결과: runs/smoke-live/<sut>-<시각>.jsonl 과 .stats.json (runs/는 git 제외).

set -euo pipefail

SUT="${1:?sut(runflow_v0|assistant_v0)를 준다}"
MODEL_ID="${2:?model_id를 준다}"
MAX_COST="${3:-0.2}"
case "$SUT" in runflow_v0|assistant_v0) ;; *) echo "알 수 없는 sut: $SUT" >&2; exit 2 ;; esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

set -a
# shellcheck disable=SC1091
source backend/dev.env
if [ ! -f secrets/.env ]; then
  echo "secrets/.env가 없다. ORBIT_OPENROUTER_API_KEY를 넣어 둔다." >&2
  exit 2
fi
# shellcheck disable=SC1091
source secrets/.env
set +a

if [ -z "${ORBIT_OPENROUTER_API_KEY:-}" ]; then
  echo "ORBIT_OPENROUTER_API_KEY가 비어 있다(secrets/.env 확인)." >&2
  exit 2
fi
export ORBIT_LLM_PROVIDER=openrouter
export ORBIT_LLM_MODEL_ID="$MODEL_ID"

mkdir -p runs/smoke-live
OUT="runs/smoke-live/${SUT}-$(date +%Y%m%d-%H%M%S).jsonl"

rc=0
uv run --directory backend python -m orbit.evalrunner run \
  --dataset ../eval/datasets/orbit-eval/v1 --split dev --subset smoke \
  --sut "$SUT" --live-llm --grade off --model-id "$MODEL_ID" --repeats 1 --seed 0 \
  --max-cost-usd "$MAX_COST" --max-total-tokens "${MAX_TOTAL_TOKENS:-1000000}" \
  --out "../$OUT" || rc=$?
if [ -f "$OUT" ]; then
  uv run --directory backend python -m orbit.evalrunner stats "../$OUT" > "${OUT%.jsonl}.stats.json" || true
  echo "결과: $OUT, 요약: ${OUT%.jsonl}.stats.json"
fi
exit "$rc"
