"""rag-v1 코퍼스의 reranker 입력 길이를 재서 `RERANKER_MAX_LENGTH`가 충분한지 확인한다(ADR-0036 A1).

사용법: cd backend && uv run --group rag-eval python ../scripts/rag_v1_reranker_tokens.py \
    ../eval/datasets/orbit-eval/rag-v1
필요 값 = (제목+본문 최대 토큰) + 질문 대표 길이 64 + 쌍 특수 토큰 4를 64 단위로 올린 값.
현재 상수보다 크면 exit 1.
"""

import json
import sys
from pathlib import Path

from transformers import AutoTokenizer

from orbit.evalrunner.rag_arch.a1 import rerank_passage
from orbit.evalrunner.rag_arch.config import (
    RERANKER_MAX_LENGTH,
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
)

QUESTION_TOKENS, SPECIAL_TOKENS, UNIT = 64, 4, 64


def max_passage_tokens(dataset_dir: Path) -> tuple[int, int]:
    tok = AutoTokenizer.from_pretrained(RERANKER_MODEL_ID, revision=RERANKER_REVISION)
    world = json.loads((dataset_dir / "world.json").read_text(encoding="utf-8"))
    best = count = 0
    for account in world["accounts"].values():
        for m in account["messages"]:
            count += 1
            mail = {"subject": m.get("subject", ""), "body": m.get("body_text", "")}
            passage = rerank_passage(mail)
            best = max(best, len(tok(passage, add_special_tokens=False)["input_ids"]))
    return best, count


def main() -> int:
    best, count = max_passage_tokens(Path(sys.argv[1]))
    total = best + QUESTION_TOKENS + SPECIAL_TOKENS
    needed = -(-total // UNIT) * UNIT
    print(f"mails={count} max_passage_tokens={best} total={total} needed_max_length={needed}")
    print(f"current RERANKER_MAX_LENGTH={RERANKER_MAX_LENGTH}")
    if needed > RERANKER_MAX_LENGTH:
        print("부족: 상수를 올려야 한다")
        return 1
    print("충분")
    return 0


if __name__ == "__main__":
    sys.exit(main())
