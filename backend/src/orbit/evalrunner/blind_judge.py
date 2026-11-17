"""Judge 보정용 균형 blind60 생성·사람 검토 화면·고정 분모 지표(ADR-0029 §3).

수동 검토한 사실 치환 manifest만 받아 결정적으로 30개를 선택한다. 원본 60개는 모두 사람
라벨이 true여야 한다. 데이터·키는 git에서 제외한 runs/에만 쓰고 네트워크를 사용하지 않는다.

    python -m orbit.evalrunner.blind_judge build --input ... --human ... --candidates ... --out ...
    python -m orbit.evalrunner.blind_judge metrics --key ... --judge ... --family google
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import subprocess
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from orbit.evaldata.v1.auto_review import read_rows, total_cost
from orbit.evalrunner import judge

DEFAULT_SEED = 20261117
KINDS = frozenset({"date", "time", "money", "name", "place"})
_SEOUL = ZoneInfo("Asia/Seoul")
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")
_KO_DATE = re.compile(r"(?:(\d{4})\s*년\s*)?(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})")
_CLOCK = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_KO_CLOCK = re.compile(r"(오전|오후)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?")
_NUMBER = re.compile(r"(?<![\d,.])(\d[\d,]*(?:\.\d+)?)\s*(억|천만|백만|십만|만|천)?")
_MULTIPLIERS = {
    "억": 100_000_000,
    "천만": 10_000_000,
    "백만": 1_000_000,
    "십만": 100_000,
    "만": 10_000,
    "천": 1000,
    None: 1,
}


class BlindError(judge.JudgeError):
    """공개 오류에는 입력 위치·ID와 검사 종류만 남긴다."""


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def fact_tokens(kind: str, text: str) -> set[str]:
    """사실 동치 표기의 보수적 대조. 의미·부정·계산 타당성은 manifest 사람 검토가 맡는다.

    날짜/시간에는 원래 ISO 오프셋과 한국 시간 표기를 함께 넣는다. 이 표본은 한국 시간 기준이며
    다른 시간대의 계산·상대시각은 자동 지원 판정 대상에 포함하지 않는다.
    """
    text = unicodedata.normalize("NFKC", text)
    found: set[str] = set()
    if kind == "money":
        for match in _NUMBER.finditer(text):
            try:
                value = Decimal(match[1].replace(",", "")) * _MULTIPLIERS[match[2]]
            except InvalidOperation:
                continue
            found.add(f"n:{value.normalize():f}")
    elif kind == "date":
        for match in _ISO_DATE.finditer(text):
            year, month, day = map(int, match.groups())
            found.update((f"ymd:{year}-{month}-{day}", f"md:{month}-{day}"))
        for match in _KO_DATE.finditer(text):
            year_text, month_text, day_text = match.groups()
            found.add(f"md:{int(month_text)}-{int(day_text)}")
            if year_text:
                found.add(f"ymd:{int(year_text)}-{int(month_text)}-{int(day_text)}")
    elif kind == "time":
        for match in _CLOCK.finditer(text):
            hour, minute = map(int, match.groups())
            if hour < 24 and minute < 60:
                found.add(f"hm:{hour}:{minute}")
        for match in _KO_CLOCK.finditer(text):
            meridiem, hour_text, minute_text = match.groups()
            hour, minute = int(hour_text), int(minute_text or 0)
            if meridiem:
                hour = hour % 12 + (12 if meridiem == "오후" else 0)
            if hour < 24 and minute < 60:
                found.add(f"hm:{hour}:{minute}")
    if kind in {"date", "time"}:
        for match in _STAMP.finditer(text):
            try:
                stamp = datetime.fromisoformat(match[0].replace("Z", "+00:00"))
            except ValueError:
                continue
            local = stamp.astimezone(_SEOUL)
            if kind == "date":
                found.update(
                    (f"ymd:{local.year}-{local.month}-{local.day}", f"md:{local.month}-{local.day}")
                )
            else:
                found.add(f"hm:{local.hour}:{local.minute}")
    return found


def fact_present(value: str, kind: str, text: str) -> bool:
    if kind in {"name", "place"}:
        return compact(value) in compact(text)
    targets = fact_tokens(kind, value)
    if kind == "date" and any(x.startswith("ymd:") for x in targets):
        targets = {x for x in targets if x.startswith("ymd:")}
    return bool(targets & fact_tokens(kind, text))


@dataclass(frozen=True)
class Mutation:
    source_id: str
    old_value: str
    new_value: str
    kind: str

    @classmethod
    def parse(cls, row: Mapping[str, Any]) -> Mutation:
        fields = ("source_id", "old_value", "new_value", "kind")
        if row.get("reviewed") is not True or any(
            not isinstance(row.get(k), str) or not row[k].strip() for k in fields
        ):
            raise BlindError("치환 후보에는 검토 확인과 네 문자열 필드가 필요하다")
        candidate = cls(*(row[k] for k in fields))
        if candidate.kind not in KINDS:
            raise BlindError(f"지원하지 않는 사실 종류: {candidate.source_id}")
        return candidate


def validate_mutation(episode: judge.Episode, mutation: Mutation) -> None:
    """답의 한 곳만 바꾸며, 모든 도구와 Judge가 실제로 볼 발췌를 각각 검사한다."""
    if episode.answer.count(mutation.old_value) != 1:
        raise BlindError(f"원래 값이 답에 정확히 한 번 있어야 한다: {episode.episode_id}")
    if compact(mutation.old_value) == compact(mutation.new_value):
        raise BlindError(f"새 값과 원래 값이 같다: {episode.episode_id}")
    full = "\n".join(result for _, result in episode.tool_results)
    keep = judge.allocate_chars([len(r) for _, r in episode.tool_results], judge.MAX_TOOL_CHARS)
    shown = "\n".join(r[:n] for (_, r), n in zip(episode.tool_results, keep, strict=True))
    if not fact_present(mutation.old_value, mutation.kind, full):
        raise BlindError(f"원래 사실의 도구 근거가 없다: {episode.episode_id}")
    if not fact_present(mutation.old_value, mutation.kind, shown):
        raise BlindError(f"원래 사실의 도구 근거가 Judge 발췌 밖이다: {episode.episode_id}")
    if fact_present(mutation.new_value, mutation.kind, full):
        raise BlindError(f"새 사실이 도구에 이미 있다: {episode.episode_id}")
    if compact(mutation.new_value) in compact(full):
        raise BlindError(f"새 값의 원문이 도구에 이미 있다: {episode.episode_id}")


def generate(
    episodes: Sequence[judge.Episode],
    human: Mapping[str, bool],
    candidates: Sequence[Mapping[str, Any]],
    seed: int = DEFAULT_SEED,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """입력 순서와 무관하게 재현된다. 같은 원본 에피소드는 원본/치환 중 한 번만 등장한다."""
    sources = {ep.episode_id: ep for ep in episodes}
    if len(episodes) != 60 or len(sources) != 60:
        raise BlindError("서로 다른 원본 에피소드 60개가 필요하다")
    if any(human.get(sid) is not True for sid in sources):
        raise BlindError("원본 60개 모두 근거 있음 사람 라벨이 필요하다")
    mutations: dict[str, Mutation] = {}
    for row in candidates:
        candidate = Mutation.parse(row)
        if candidate.source_id not in sources or candidate.source_id in mutations:
            raise BlindError(f"치환 후보의 source_id가 없거나 겹친다: {candidate.source_id}")
        validate_mutation(sources[candidate.source_id], candidate)
        mutations[candidate.source_id] = candidate
    if len(mutations) < 30:
        raise BlindError("검토된 사실 치환 후보가 30개 이상 필요하다")
    rng = random.Random(seed)  # noqa: S311  재현 가능한 표본/순서 생성용이다.
    selected = set(rng.sample(sorted(mutations), 30))
    order = sorted(sources)
    rng.shuffle(order)
    inputs: list[dict[str, Any]] = []
    keys: list[dict[str, Any]] = []
    for index, sid in enumerate(order, start=1):
        ep = sources[sid]
        mutation = mutations[sid] if sid in selected else None
        task_id = f"item-{index:02d}"
        inputs.append(
            {
                "sut": "blind",
                "task_id": task_id,
                "repeat": 1,
                "cluster_id": task_id,
                "question": ep.question,
                "answer_text": ep.answer.replace(mutation.old_value, mutation.new_value, 1)
                if mutation
                else ep.answer,
                "tool_results": [{"name": name, "result": body} for name, body in ep.tool_results],
            }
        )
        keys.append(
            {
                "id": f"blind/{task_id}/1",
                "mutated": mutation is not None,
                "old_value": mutation.old_value if mutation else None,
                "new_value": mutation.new_value if mutation else None,
                "kind": mutation.kind if mutation else None,
                "source_id": sid,
            }
        )
    return inputs, keys


def _leaves(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        return [
            line
            for key, child in value.items()
            for line in _leaves(child, f"{prefix}.{key}" if prefix else str(key))
        ]
    if isinstance(value, list):
        return [line for i, child in enumerate(value) for line in _leaves(child, f"{prefix}[{i}]")]
    return [(prefix, str(value))]


def evidence_hints(answer: str, tools: Sequence[Mapping[str, Any]]) -> list[str]:
    """모든 문항에 같은 방식으로 어휘가 겹치는 근거 후보를 보여 준다. 판정·경고는 만들지 않는다."""
    words = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", answer.casefold()))
    numbers = fact_tokens("money", answer)
    lines: list[tuple[str, str]] = []
    for tool in tools:
        body = str(tool.get("result", ""))
        try:
            values = _leaves(json.loads(body))
        except (ValueError, TypeError):
            values = [("", line) for line in body.splitlines()]
        for prefix, value in values:
            for line in re.split(r"\n|(?<=[.!?])\s+", value):
                if line.strip():
                    shown = f"{prefix}: {line.strip()}" if prefix else line.strip()
                    lines.append((f"{tool.get('name', '')}: {shown}", line.strip()))
    scored = [
        (
            len(words & set(re.findall(r"[가-힣A-Za-z0-9]{2,}", content.casefold())))
            + len(numbers & fact_tokens("money", content)),
            index,
            line,
        )
        for index, (line, content) in enumerate(lines)
    ]
    ranked = sorted((item for item in scored if item[0] > 0), key=lambda item: (-item[0], item[1]))
    return [line[:800] for _, _, line in ranked[:6]]


def labeler_projection(inputs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """정답 키를 받지 않는 allowlist 투영. 원본 메타데이터를 UI 데이터에 섞지 않는다."""
    projected: list[dict[str, Any]] = []
    for index, row in enumerate(inputs, start=1):
        tools = [
            {"name": str(t.get("name", "")), "result": str(t.get("result", ""))}
            for t in row.get("tool_results", [])
        ]
        answer = str(row.get("answer_text", ""))
        projected.append(
            {
                "n": index,
                "question": str(row.get("question", "")),
                "answer": answer,
                "hints": evidence_hints(answer, tools),
                "tools": tools,
            }
        )
    return projected


def render_html(inputs: Sequence[Mapping[str, Any]], tokens_css: str) -> str:
    # JSON script 안의 닫는 태그·HTML 파서를 무력화한다. DOM에는 textContent만 쓴다.
    payload = json.dumps(labeler_projection(inputs), ensure_ascii=False)
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    payload = payload.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    if re.search(r"@import|url\s*\(|</style", tokens_css, re.IGNORECASE):
        raise BlindError("토큰 CSS에 외부 리소스나 style 종료 태그를 넣을 수 없다")
    digest = hashlib.sha256(payload.encode()).hexdigest()[:16]
    return (
        _HTML.replace("__TOKENS__", tokens_css)
        .replace("__STORAGE__", f"orbit-blind60-{digest}")
        .replace("__DATA__", payload)
    )


def score_key(
    keys: Sequence[Mapping[str, Any]], predictions: Mapping[str, bool | None]
) -> dict[str, Any]:
    ids = [str(row["id"]) for row in keys]
    if len(keys) != 60 or len(set(ids)) != 60:
        raise BlindError("정답 키는 중복 없는 60문항이어야 한다")
    if (
        sum(row.get("mutated") is True for row in keys) != 30
        or sum(row.get("mutated") is False for row in keys) != 30
    ):
        raise BlindError("정답 키는 원본 30개와 치환 30개여야 한다")
    correct = {True: 0, False: 0}
    missing = {True: 0, False: 0}
    false_positives = 0
    for row in keys:
        expected = not row["mutated"]
        prediction = predictions.get(str(row["id"]))
        if prediction is None:
            missing[expected] += 1
        elif type(prediction) is not bool:
            raise BlindError("Judge 판정은 bool 또는 None이어야 한다")
        elif prediction == expected:
            correct[expected] += 1
        elif expected:
            false_positives += 1
    return {
        "n": 60,
        "supported_n": 30,
        "unsupported_n": 30,
        "supported_recall": correct[True] / 30,
        "unsupported_recall": correct[False] / 30,
        "false_alarm_rate": false_positives / 30,
        "accuracy": sum(correct.values()) / 60,
        "correct": sum(correct.values()),
        "missing": sum(missing.values()),
        "missing_supported": missing[True],
        "missing_unsupported": missing[False],
        "human_kappa": "pending",
    }


def _rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise BlindError(f"JSONL 형식 오류: {path.name}:{number}") from None
        if not isinstance(row, dict):
            raise BlindError(f"JSONL 객체가 필요하다: {path.name}:{number}")
        rows.append(row)
    return rows


def write_bundle(
    out: Path,
    inputs: Sequence[Mapping[str, Any]],
    keys: Sequence[Mapping[str, Any]],
    tokens_css: str,
    seed: int,
    suite_summary: Mapping[str, Any] | None = None,
) -> None:
    out = out.resolve()
    if "runs" not in out.parts:
        raise BlindError("출력은 git에서 제외한 runs/ 아래여야 한다")
    probe = subprocess.run(  # noqa: S603  고정 명령, shell 없이 경로만 전달한다.
        ["git", "-C", str(out.parent), "check-ignore", "-q", str(out)],  # noqa: S607
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        raise BlindError("출력 디렉터리가 git 제외 경로인지 확인할 수 없다")
    data = {
        "blind60-input.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in inputs),
        "blind60-key.jsonl": "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keys),
        "blind60.html": render_html(inputs, tokens_css),
        "blind60-manifest.json": json.dumps(
            {
                "seed": seed,
                "n": 60,
                "originals": 30,
                "mutations": 30,
                "judge_prompt_version": judge.PROMPT_VERSION,
                **({"suite": dict(suite_summary)} if suite_summary is not None else {}),
                "input_sha256": hashlib.sha256(
                    json.dumps(list(inputs), sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    }
    if suite_summary is not None:
        data["blind60-summary.json"] = (
            json.dumps(dict(suite_summary), ensure_ascii=False, indent=2) + "\n"
        )
    if any((out / name).exists() for name in data):
        raise BlindError("이미 생성된 파일이 있다. 다른 출력 디렉터리를 고른다")
    out.mkdir(parents=True, exist_ok=True)
    for name, text in data.items():
        (out / name).write_text(text, "utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command, seed in (("build", DEFAULT_SEED), ("build-v3", 20261118)):
        build = commands.add_parser(command)
        for field in ("input", "human", "candidates", "out"):
            build.add_argument(f"--{field}", type=Path, required=True)
        build.add_argument("--seed", type=int, default=seed)
        build.add_argument(
            "--tokens",
            type=Path,
            default=Path(__file__).resolve().parents[4] / "docs/brand/tokens.css",
        )
    metrics = commands.add_parser("metrics")
    metrics.add_argument("--key", type=Path, required=True)
    metrics.add_argument("--judge", type=Path, required=True)
    metrics.add_argument("--family")
    baseline = commands.add_parser("baseline-v3")
    baseline.add_argument("--input", type=Path, required=True)
    baseline.add_argument("--key", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command in {"build", "build-v3"}:
            episodes, skipped = judge.load_episodes(args.input)
            if skipped:
                raise BlindError("답이 없는 원본을 포함할 수 없다")
            suite_summary = None
            if args.command == "build-v3":
                from orbit.evalrunner import blind_judge_v3 as v3

                inputs, keys, suite_summary = v3.generate(
                    episodes, judge.load_human(args.human), _rows(args.candidates), args.seed
                )
            else:
                inputs, keys = generate(
                    episodes, judge.load_human(args.human), _rows(args.candidates), args.seed
                )
            write_bundle(
                args.out, inputs, keys, args.tokens.read_text("utf-8"), args.seed, suite_summary
            )
            print(json.dumps({"n": 60, "originals": 30, "mutations": 30, "seed": args.seed}))
        elif args.command == "baseline-v3":
            from orbit.evalrunner import blind_judge_v3 as v3

            report = v3.score_key(_rows(args.key), v3.baseline_predictions(_rows(args.input)), 0.0)
            report["baseline_rules"] = v3.BASELINE_RULES
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            judgments = judge.load_judgments(args.judge, args.family)
            keys = _rows(args.key)
            predictions = {k: v.grounded for k, v in judgments.items()}
            file = args.judge / judge.JUDGE_FILE if args.judge.is_dir() else args.judge
            costs = total_cost(
                [
                    r
                    for r in read_rows(file)
                    if args.family is None or r.get("reviewer_family") == args.family
                ]
            )
            if any(
                row.get("kind")
                in {
                    "surface",
                    "stale_value",
                    "role_swap",
                    "unsupported_addition",
                    "false_action",
                    "false_absence",
                    "meaning_flip",
                }
                for row in keys
            ):
                from orbit.evalrunner import blind_judge_v3 as v3

                report = v3.score_key(keys, predictions, costs)
            else:
                report = {**score_key(keys, predictions), "cost_usd": costs}
            print(json.dumps(report, ensure_ascii=False, indent=2))
    except (BlindError, judge.JudgeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


_HTML = Path(__file__).with_suffix(".html").read_text("utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
