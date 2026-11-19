#!/usr/bin/env bash
# ADR-0035. Keys are sourced in-process only; never print the environment.
set -euo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_COMMON_GIT="$(git -C "$TASK_ROOT" rev-parse --path-format=absolute --git-common-dir)"
TASK_SECRETS_FILE="${ORBIT_EVAL_SECRETS_FILE:-$(dirname "$TASK_COMMON_GIT")/secrets/.env}"
set -a
source "$TASK_ROOT/backend/dev.env"
source "$TASK_SECRETS_FILE"
set +a
export ORBIT_LLM_PROVIDER=openai
export ORBIT_LLM_MODEL_ID=openai/gpt-6-luna
export ORBIT_LLM_REASONING_EFFORT=none
export ORBIT_LLM_PRICE_SOURCE='https://openrouter.ai/api/v1/models (snapshot 2026-11-19; configured standard-tier rates)'
export ORBIT_LLM_PRICE_INPUT_PER_MTOK=0.10
export ORBIT_LLM_PRICE_CACHED_INPUT_PER_MTOK=0.01
export ORBIT_LLM_PRICE_OUTPUT_PER_MTOK=0.50
cd "$TASK_ROOT"
exec "$TASK_ROOT/backend/.venv/bin/python" scripts/eval-rag-patterns.py "$@"
