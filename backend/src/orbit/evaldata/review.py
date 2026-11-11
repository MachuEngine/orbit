"""사람 검수 도구 (ADR-0009 2절: v0은 전수 검수).

    python -m orbit.evaldata.review export <dataset> --out <검수표.md>
    python -m orbit.evaldata.review apply <dataset> <decisions.jsonl>

`decisions.jsonl`은 한 줄에 `{"task_id": "...", "decision": "approve|fix|exclude",
"note": "..."}` 형식이다.
`apply`는 `review.jsonl`을 기록하고 manifest의 `notes`에 검수 현황을 갱신한다.
제외(`exclude`)된 과제는 `load_dataset`이 기본으로 건너뛴다. `fix`는 고칠 때까지 포함되며,
고친 뒤 다시 `approve`를 기록한다.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from orbit.evaldata.dataset import (
    REVIEW,
    DatasetError,
    describe_error,
    dumps_jsonl,
    load_dataset,
    read_review,
    write_manifest,
)
from orbit.evaldata.schema import ReviewEntry, Task
from orbit.evaldata.timeutil import KST, weekday_ko

_REVIEW_NOTE_PREFIX = "검수 현황:"


def _kst(iso: str) -> str:
    local = datetime.fromisoformat(iso).astimezone(KST)
    return f"{local:%m-%d}({weekday_ko(local.date())}) {local:%H:%M}"


def _event_line(ev: dict[str, Any]) -> str:
    attendees = ", ".join(a.get("name") or a["email"] for a in ev["attendees"])
    return (
        f"- `{ev['id']}` {ev['title']} | {_kst(ev['start'])}~{_kst(ev['end'])[-5:]} KST | "
        f"{ev['status']} | 참석: {attendees}"
    )


def _mail_line(m: dict[str, Any]) -> str:
    sender = m["from"].get("name") or m["from"]["email"]
    return f"- `{m['id']}` {_kst(m['sent_at'])} | {sender} | {m['subject']} | {m['snippet']}"


def _task_type(task: Task) -> str:
    return next((t.split(":", 1)[1] for t in task.tags if t.startswith("type:")), "?")


def export_markdown(dataset_dir: str | Path) -> str:
    ds = load_dataset(dataset_dir, include_excluded=True)
    events: dict[str, dict[str, Any]] = {}
    mails: dict[str, dict[str, Any]] = {}
    for acct in ds.world_dict["accounts"].values():
        events.update({e["id"]: e for e in acct["events"]})
        mails.update({m["id"]: m for m in acct["messages"]})
    lines = [
        f"# 검수표: {ds.manifest.name}@{ds.manifest.version} (과제 {len(ds.tasks)}개)",
        "",
        "과제마다 질문·정답 표현·근거를 확인하고 판단을 `decisions.jsonl`에 한 줄씩 적는다.",
        '형식: `{"task_id": "day-001", "decision": "approve", "note": ""}` '
        "(decision은 approve, fix, exclude 중 하나).",
        "정답 표현은 응답에 하나라도(공백 무시) 포함되면 맞는 것으로 본다.",
        "",
    ]
    for t in ds.tasks:
        gold = t.gold
        lines += [
            f"## {t.task_id} ({_task_type(t)}, {t.split})",
            f"- 시나리오: {t.scenario_id} / 계정: {t.account_id}",
            f"- 기준 시각(as_of_time): {t.as_of_time.isoformat()}",
            f"- 질문: {t.turns[0].content}",
            "- 정답 표현(any_of 중 하나):",
        ]
        lines += [
            f"  - {f.fact_id}: {' / '.join(f'`{x}`' for x in f.any_of)} ({f.description})"
            for f in gold.answer_facts
        ]
        if gold.must_not_contain:
            lines.append(
                "- 응답에 있으면 안 되는 표현: "
                + ", ".join(f"`{x}`" for x in gold.must_not_contain)
            )
        lines.append("- 난이도: " + ", ".join(f"{k}={v}" for k, v in sorted(t.difficulty.items())))
        ref_events = [events[x.split(":", 1)[1]] for x in t.tags if x.startswith("ref_event:")]
        ref_mails = [mails[x.split(":", 1)[1]] for x in t.tags if x.startswith("ref_message:")]
        lines.append("- 근거 일정:")
        lines += [f"  {_event_line(e)}" for e in ref_events]
        lines.append("- 근거 메일:")
        lines += [f"  {_mail_line(m)}" for m in ref_mails]
        prior = ds.review.get(t.task_id)
        if prior is not None:
            lines.append(f"- 기존 판단: {prior.decision} {prior.note}".rstrip())
        lines += ["- 판단: [ ] approve  [ ] fix  [ ] exclude", "- 메모:", ""]
    return "\n".join(lines)


def _read_decisions(path: Path) -> list[ReviewEntry]:
    entries: list[ReviewEntry] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DatasetError(f"결정 파일을 읽을 수 없다: {exc}") from exc
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entries.append(ReviewEntry.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise DatasetError(
                f"결정 파일 {lineno}번째 줄이 올바르지 않다: {describe_error(exc)}"
            ) from exc
    return entries


def apply_decisions(dataset_dir: str | Path, decisions_path: str | Path) -> dict[str, ReviewEntry]:
    """결정을 review.jsonl에 합치고 manifest.notes를 갱신한다. 같은 과제는 새 결정이 이긴다."""
    root = Path(dataset_dir)
    ds = load_dataset(root, include_excluded=True)
    known = {t.task_id for t in ds.tasks}
    new = _read_decisions(Path(decisions_path))
    unknown = sorted({e.task_id for e in new} - known)
    if unknown:
        raise DatasetError(f"데이터셋에 없는 task_id: {', '.join(unknown)}")
    merged = {**read_review(root), **{e.task_id: e for e in new}}
    rows = [merged[k].model_dump(mode="json") for k in sorted(merged)]
    (root / REVIEW).write_text(dumps_jsonl(rows), encoding="utf-8")
    counts = Counter(e.decision for e in merged.values())
    summary = (
        f"{_REVIEW_NOTE_PREFIX} 검수 {len(merged)}/{len(known)}과제 "
        f"(approve {counts['approve']}, fix {counts['fix']}, exclude {counts['exclude']})"
    )
    base = "\n".join(
        ln for ln in ds.manifest.notes.splitlines() if not ln.startswith(_REVIEW_NOTE_PREFIX)
    )
    notes = f"{base}\n{summary}" if base else summary
    write_manifest(root, ds.manifest.model_copy(update={"notes": notes}))
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Orbit 평가 데이터 검수 도구")
    sub = parser.add_subparsers(dest="cmd", required=True)
    exp = sub.add_parser("export", help="검수표(Markdown) 내보내기")
    exp.add_argument("dataset")
    exp.add_argument("--out", required=True)
    app = sub.add_parser("apply", help="결정(JSONL)을 review.jsonl에 기록")
    app.add_argument("dataset")
    app.add_argument("decisions")
    args = parser.parse_args(argv)
    if args.cmd == "export":
        Path(args.out).write_text(export_markdown(args.dataset), encoding="utf-8")
        print(f"검수표 작성: {args.out}")
    else:
        merged = apply_decisions(args.dataset, args.decisions)
        print(f"review.jsonl 기록: {len(merged)}건")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
