"""safety-v1 생성 CLI.

```bash
cd backend
uv run python -m orbit.evaldata.safety_v1 build [--seed N] [--out DIR] [--git-sha SHA]
uv run python -m orbit.evaldata.safety_v1 stats [--out DIR]
uv run python -m orbit.evaldata.safety_v1 seal --review-note 문자열
uv run python -m orbit.evaldata.safety_v1 verify-seal
```

기본 출력은 `eval/datasets/orbit-eval/safety-v1/`다. LLM을 쓰지 않으며 같은 시드·git SHA면 산출물은
바이트가 같다. 봉인된 디렉터리에는 `build`가 쓰지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from orbit.evaldata import dataset as ds
from orbit.evaldata.generate import git_sha
from orbit.evaldata.safety_v1 import seal
from orbit.evaldata.safety_v1.build import DEFAULT_SEED, build
from orbit.evaldata.safety_v1.review import PAIR_REVIEW

DEFAULT_DIR = Path(__file__).resolve().parents[5] / "eval" / "datasets" / "orbit-eval" / "safety-v1"


def cmd_build(args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    out = Path(args.out)
    manifest_path = out / ds.MANIFEST
    if manifest_path.exists() and '"seal"' in manifest_path.read_text(encoding="utf-8"):
        print("실패: 봉인된 데이터셋이라 다시 쓰지 않는다", file=sys.stderr)
        return 2
    sha = args.git_sha if args.git_sha is not None else git_sha(Path(__file__).parent)
    built = build(args.seed, sha)
    ds.write_dataset(out, built.manifest, built.world, built.tasks)
    review_path = out / PAIR_REVIEW
    if not review_path.exists():  # 사람이 채운 검수 기록을 덮어쓰지 않는다
        review_path.write_text(ds.dumps_jsonl(built.review), encoding="utf-8")
    c = built.manifest.counts
    echo(
        f"safety-v1 생성: 계정 {c.users}, 메일 {c.messages}, 일정 {c.events}, "
        f"과제 {c.tasks} -> {out}"
    )
    return 0


def cmd_stats(args: argparse.Namespace, echo: Callable[[str], None]) -> int:
    ds_ = ds.load_dataset(args.out, include_excluded=True)
    details = ds_.manifest.details or {}
    echo(json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None, echo: Callable[[str], None] = print) -> int:
    parser = argparse.ArgumentParser(prog="orbit.evaldata.safety_v1")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="데이터셋 생성")
    b.add_argument("--seed", type=int, default=DEFAULT_SEED)
    b.add_argument("--git-sha", default=None)
    s = sub.add_parser("stats", help="유형·split·채널·디스트랙터 수")
    sl = sub.add_parser("seal", help="test 봉인(쌍 검수가 끝나야 한다)")
    sl.add_argument("--review-note", required=True)
    v = sub.add_parser("verify-seal", help="봉인 일치 확인")
    for p in (b, s, sl, v):
        p.add_argument("--out", default=str(DEFAULT_DIR))
    args = parser.parse_args(argv)
    handlers: dict[str, Callable[[argparse.Namespace, Callable[[str], None]], int]] = {
        "build": cmd_build,
        "stats": cmd_stats,
        "seal": seal.cmd_seal,
        "verify-seal": seal.cmd_verify,
    }
    return handlers[args.cmd](args, echo)
