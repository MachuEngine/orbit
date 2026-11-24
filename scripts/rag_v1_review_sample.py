"""rag-v1 사람 검수용 표본 꾸러미를 만든다 (ADR-0036 사람 검수).

규칙: 질문 유형(rag:single·multihop·aggregate·unanswerable)마다 10개, 계정 4개에 고르게
돌아가며 뽑는다. 시드는 고정해 같은 입력이면 같은 표본이 나온다.
근거 메일은 review-packet.md와 달리 본문 전체와 머리글을 싣는다.

사용: python3 scripts/rag_v1_review_sample.py [데이터셋 디렉터리]
"""

import json
import random
import re
import sys
from pathlib import Path

SEED = 20261121
PER_TYPE = 10
TYPES = ["single", "multihop", "aggregate", "unanswerable"]


def main(root: Path) -> None:
    tasks = [json.loads(line) for line in (root / "tasks.jsonl").open(encoding="utf-8")]
    world = json.loads((root / "world.json").read_text(encoding="utf-8"))
    msgs = {m["id"]: m for acct in world["accounts"].values() for m in acct["messages"]}

    # review-packet.md의 문항별 요약(질문·정답·숨긴 중간 값)을 그대로 재사용한다.
    packet = (root / "review-packet.md").read_text(encoding="utf-8")
    summary = {}
    for sec in re.split(r"\n(?=## )", packet)[1:]:
        head, *lines = sec.splitlines()
        tid = head[3:].split(" ")[0]
        summary[tid] = (head, [ln for ln in lines if ln.startswith("- ")])

    rng = random.Random(SEED)
    picked = []
    for typ in TYPES:
        by_acct: dict[str, list] = {}
        for t in sorted(tasks, key=lambda x: x["task_id"]):
            if f"rag:{typ}" in t["tags"]:
                by_acct.setdefault(t["account_id"], []).append(t)
        for lst in by_acct.values():
            rng.shuffle(lst)
        queues = [by_acct[a] for a in sorted(by_acct)]
        chosen = []
        while len(chosen) < PER_TYPE and any(queues):
            for q in queues:
                if q and len(chosen) < PER_TYPE:
                    chosen.append(q.pop())
        picked.extend(sorted(chosen, key=lambda x: x["task_id"]))

    out = [
        "# rag-v1 사람 검수 표본",
        "",
        f"- 표본 규칙: 질문 유형마다 {PER_TYPE}개, 계정별로 돌아가며 추출, 시드 {SEED}"
        " (`scripts/rag_v1_review_sample.py`)",
        f"- 문항 {len(picked)}개 / 전체 {len(tasks)}개",
        "",
        "## 검수 방법",
        "",
        "문항마다 아래 세 가지를 확인하고 `판정` 줄에 적는다. 문제가 없으면 `OK`, 있으면 이유를 쓴다.",
        "",
        "1. 질문이 한 가지 뜻으로만 읽히는가",
        "2. 근거 메일만 읽고 정답을 확정할 수 있는가 (답할 수 없음 유형은 메일에 정말 없는가)",
        "3. 질문 문장에 정답이나 숨긴 중간 값이 드러나지 않는가",
        "",
    ]
    for t in picked:
        head, lines = summary[t["task_id"]]
        out += ["---", "", head, *[ln for ln in lines if not ln.startswith("  ")]]
        mnc = t["gold"].get("must_not_contain") or []
        if mnc:
            out.append(f"- 답변에 나오면 안 되는 표현: {', '.join(map(str, mnc))}")
        out += ["- 판정: ", ""]
        for ref in (t["gold"].get("expected_answer") or {}).get("messages") or []:
            m = msgs[ref["message_id"]]
            to = ", ".join(r.get("email", "") for r in m.get("to", []))
            out += [
                f"#### `{m['id']}` {m.get('subject', '')}",
                f"보낸 사람: {m['from'].get('name', '')} <{m['from']['email']}> · 받는 사람: {to}"
                f" · 보낸 시각: {m.get('sent_at', '')}",
                "",
                *["> " + ln if ln else ">" for ln in m["body_text"].splitlines()],
                "",
            ]
    (root / "review-sample.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"review-sample.md: {len(picked)}문항", [t["task_id"] for t in picked])


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "eval/datasets/orbit-eval/rag-v1"))
