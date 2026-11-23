"""완료된 평가의 Judge 입력 준비와 엄격한 사후 집계. LLM·DB 호출은 하지 않는다.

    python -m orbit.evalrunner.judge_stats prepare --manifest settings.json --out batch.jsonl
    python -m orbit.evalrunner.judge_stats summarize --manifest settings.json --judge DIR \
        --family google --model-id google/gemini-3.6-flash --out summary.json

manifest의 각 setting은 id/results/config_fingerprint를 가진다. 기본 기대치는 test 130과제,
102묶음·1회다. 서로 다른 설정의 같은 에피소드는 setting_id::sut/task/repeat로 구분한다.
입력에는 오류 도구 기록도 포함한다. primary 성공 판정·코드 사실 근거율의 기존 경로는 유지한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from orbit.evalrunner import judge, metrics_v1, stats

RESAMPLES = 10_000
SEED = 0
_SETTING_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


class JudgeStatsError(judge.JudgeError):
    """원문 없이 설정 ID·위치·검사 종류만 담는 집계 오류."""


def config_digest(config: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(config), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _read_rows(path: Path, *, data: bytes | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        text = (path.read_bytes() if data is None else data).decode("utf-8")
    except UnicodeError:
        raise JudgeStatsError(f"UTF-8 형식 오류: {path.name}") from None
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise JudgeStatsError(f"JSONL 오류: {path.name}:{line_no}") from None
        if not isinstance(row, dict):
            raise JudgeStatsError(f"객체가 아닌 행: {path.name}:{line_no}")
        rows.append(row)
    return rows


def _subset(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        return isinstance(actual, Mapping) and all(
            key in actual and _subset(actual[key], value) for key, value in expected.items()
        )
    return type(actual) is type(expected) and actual == expected


def _key(row: Mapping[str, Any]) -> tuple[str, int]:
    task, repeat = row.get("task_id"), row.get("repeat")
    if not isinstance(task, str) or not task or type(repeat) is not int or repeat != 1:
        raise JudgeStatsError("task_id 또는 test 반복 번호가 잘못됐다")
    return task, repeat


@dataclass(frozen=True)
class Setting:
    id: str
    config_fingerprint: str
    records: tuple[Mapping[str, Any], ...]
    results_sha256: str


@dataclass(frozen=True)
class Manifest:
    baseline: str
    settings: tuple[Setting, ...]
    expected_tasks: int
    expected_clusters: int
    expected_settings: int = 12


def load_manifest(path: Path) -> Manifest:
    try:
        raw = json.loads(path.read_text("utf-8"))
    except ValueError:
        raise JudgeStatsError("manifest JSON 오류") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("settings"), list):
        raise JudgeStatsError("manifest settings 목록이 필요하다")
    expected_tasks, expected_clusters = (
        raw.get("expected_tasks", 130),
        raw.get("expected_clusters", 102),
    )
    expected_settings = raw.get("expected_settings", 12)
    if any(
        type(value) is not int or value < 1
        for value in (expected_tasks, expected_clusters, expected_settings)
    ):
        raise JudgeStatsError("기대 과제·묶음 수는 양의 정수여야 한다")
    settings: list[Setting] = []
    seen: set[str] = set()
    for spec in raw["settings"]:
        if not isinstance(spec, dict):
            raise JudgeStatsError("setting 객체가 필요하다")
        sid, fingerprint = spec.get("id"), spec.get("config_fingerprint")
        if not isinstance(sid, str) or not _SETTING_ID.fullmatch(sid) or sid in seen:
            raise JudgeStatsError("setting ID가 잘못됐거나 겹친다")
        seen.add(sid)
        if (
            not isinstance(fingerprint, str)
            or not fingerprint
            or not isinstance(spec.get("results"), str)
        ):
            raise JudgeStatsError(f"결과 경로·지문이 필요하다: {sid}")
        source = Path(spec["results"])
        source = source if source.is_absolute() else path.parent / source
        file_hash = spec.get("results_sha256")
        if not isinstance(file_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", file_hash):
            raise JudgeStatsError(f"결과 원본 bytes SHA256가 필요하다: {sid}")
        source_bytes = source.read_bytes()
        if hashlib.sha256(source_bytes).hexdigest() != file_hash:
            raise JudgeStatsError(f"결과 원본 bytes SHA256가 다르다: {sid}")
        rows = _read_rows(source, data=source_bytes)  # 검증한 bytes 그대로 파싱한다.
        validate_records(
            sid,
            rows,
            fingerprint,
            expected_tasks,
            expected_clusters,
            spec.get("expected_config", {}),
        )
        settings.append(Setting(sid, fingerprint, tuple(rows), file_hash))
    if not settings:
        raise JudgeStatsError("setting이 비었다")
    # prepare의 새 5개 배치는 baseline을 포함하지 않아도 된다. summarize에서 존재를 강제한다.
    baseline = raw.get("baseline", "luna-index")
    if not isinstance(baseline, str) or not _SETTING_ID.fullmatch(baseline):
        raise JudgeStatsError("baseline ID가 잘못됐다")
    reference = settings[0]
    mapping = {_key(row): row["cluster_id"] for row in reference.records}
    dataset = reference.records[0]["config"]["dataset"]
    for setting in settings[1:]:
        if {_key(row): row["cluster_id"] for row in setting.records} != mapping:
            raise JudgeStatsError(f"설정 사이 과제·묶음 대응이 다르다: {setting.id}")
        if setting.records[0]["config"]["dataset"] != dataset:
            raise JudgeStatsError(f"설정 사이 데이터셋이 다르다: {setting.id}")
    return Manifest(baseline, tuple(settings), expected_tasks, expected_clusters, expected_settings)


def validate_records(
    sid: str,
    rows: Sequence[Mapping[str, Any]],
    fingerprint: str,
    expected_tasks: int,
    expected_clusters: int,
    expected_config: Any = None,
) -> None:
    if len(rows) != expected_tasks:
        raise JudgeStatsError(f"기대 결과 수와 다르다: {sid}")
    seen: set[tuple[str, int]] = set()
    configs: set[str] = set()
    clusters: set[str] = set()
    if expected_config is not None and not isinstance(expected_config, Mapping):
        raise JudgeStatsError(f"expected_config는 객체여야 한다: {sid}")
    for row in rows:
        key = _key(row)
        if key in seen:
            raise JudgeStatsError(f"중복 에피소드: {sid}")
        seen.add(key)
        cid, config = row.get("cluster_id"), row.get("config")
        if not isinstance(cid, str) or not cid or not isinstance(config, Mapping):
            raise JudgeStatsError(f"묶음·설정 기록이 없다: {sid}")
        clusters.add(cid)
        digest = config_digest(config)
        configs.add(digest)
        if row.get("config_fingerprint") != fingerprint or digest != fingerprint:
            raise JudgeStatsError(f"원본 설정 지문 또는 내용이 다르다: {sid}")
        if (
            config.get("split") != "test"
            or type(config.get("repeats")) is not int
            or type(config.get("n_tasks")) is not int
            or config.get("repeats") != 1
            or config.get("n_tasks") != expected_tasks
        ):
            raise JudgeStatsError(f"완료된 test 1회 설정이 아니다: {sid}")
        if not isinstance(config.get("dataset"), str) or not isinstance(config.get("sut"), Mapping):
            raise JudgeStatsError(f"데이터셋·SUT 설정이 없다: {sid}")
        if not isinstance(config["sut"].get("name"), str) or not config["sut"]["name"]:
            raise JudgeStatsError(f"SUT 이름이 없다: {sid}")
        if expected_config is not None and not _subset(config, expected_config):
            raise JudgeStatsError(f"선언한 설정과 다르다: {sid}")
        outcome = row.get("outcome")
        if (
            not isinstance(outcome, Mapping)
            or "success" not in outcome
            or (outcome["success"] is not None and type(outcome["success"]) is not bool)
        ):
            raise JudgeStatsError(f"성공 판정 형식이 다르다: {sid}")
        if row.get("model_mismatch"):
            raise JudgeStatsError(f"실제 응답 모델 불일치: {sid}")
    if len(configs) != 1 or len(clusters) != expected_clusters:
        raise JudgeStatsError(f"기대 설정·묶음 수와 다르다: {sid}")


def prepare_record(sid: str, record: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """빈 목록과 누락된 기록을 구별한다. 오류 종류·본문은 실제 기록된 값만 데이터로 옮긴다."""
    trace = record.get("trace")
    if not isinstance(trace, Mapping):
        return None, "no_judge_recording"
    answer = trace.get("final_text")
    if not isinstance(answer, str) or not answer.strip():
        return None, "no_answer"
    if not isinstance(trace.get("tool_calls"), list) or not isinstance(trace.get("turns"), list):
        return None, "no_judge_recording"
    users = []
    for turn in trace["turns"]:
        if not isinstance(turn, Mapping):
            raise JudgeStatsError(f"턴 기록 형식 오류: {sid}")
        if isinstance(turn.get("user"), str):
            users.append(turn["user"])
    tools = []
    for call in trace["tool_calls"]:
        if not isinstance(call, Mapping) or not isinstance(call.get("name"), str):
            raise JudgeStatsError(f"도구 기록 형식 오류: {sid}")
        if call["name"] in metrics_v1.IGNORED_TOOLS:
            continue
        if not isinstance(call.get("result"), str):
            return None, "no_judge_recording"
        body = call["result"]
        kind = call.get("error_kind")
        if call.get("is_error") and isinstance(kind, str):
            body = json.dumps(
                {"recorded_result": body, "recorded_error_kind": kind}, ensure_ascii=False
            )
        tools.append({"name": call["name"], "result": body})
    sut = record["config"]["sut"]["name"]
    meta = record.get("task_meta")
    return {
        "task_id": record["task_id"],
        "repeat": record["repeat"],
        "sut": f"{sid}::{sut}",
        "source_sut": sut,
        "setting_id": sid,
        "source_config_fingerprint": record["config_fingerprint"],
        "task_type": meta.get("type") if isinstance(meta, Mapping) else None,
        "cluster_id": record["cluster_id"],
        "question": "\n".join(users),
        "answer_text": answer,
        "tool_results": tools,
    }, None


def as_episode(row: Mapping[str, Any]) -> judge.Episode:
    return judge.Episode(
        judge.episode_id_of(row),
        str(row["task_id"]),
        row["repeat"],
        str(row["sut"]),
        row.get("task_type"),
        str(row["cluster_id"]),
        str(row["question"]),
        str(row["answer_text"]),
        tuple((tool["name"], tool["result"]) for tool in row["tool_results"]),
    )


def prepare(manifest: Manifest) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    counts: dict[str, Any] = {}
    for setting in manifest.settings:
        reasons: Counter[str] = Counter()
        eligible = 0
        for record in setting.records:
            row, reason = prepare_record(setting.id, record)
            if row is not None:
                inputs.append(row)
                eligible += 1
            else:
                reasons[str(reason)] += 1
        counts[setting.id] = {
            "total": len(setting.records),
            "eligible": eligible,
            "excluded": dict(sorted(reasons.items())),
            "config_fingerprint": setting.config_fingerprint,
            "results_sha256": setting.results_sha256,
        }
    return inputs, counts


def _history(
    rows: Sequence[Mapping[str, Any]],
    expected: Mapping[str, tuple[judge.Episode, str]],
    family: str,
    model_id: str,
) -> tuple[dict[str, Mapping[str, Any]], dict[str, dict[str, Any]]]:
    """aborted*→ok 1회만 정상 재개다. 입력·버전·모델이 다른 이력이나 완료 중복은 거절한다."""
    latest: dict[str, Mapping[str, Any]] = {}
    meta: dict[str, dict[str, Any]] = {}
    model_config_sha: str | None = None
    for row in rows:
        eid = row.get("item_id")
        if not isinstance(eid, str) or eid not in expected:
            raise JudgeStatsError("Judge 이력에 알 수 없는 namespace/에피소드가 있다")
        ep, sid = expected[eid]
        if row.get("reviewer_family") != family or row.get("model_id") != model_id:
            raise JudgeStatsError(f"Judge 모델·계열이 다르다: {sid}")
        config = row.get("model_config")
        if not isinstance(config, Mapping) or config.get("model_id") != model_id:
            raise JudgeStatsError(f"Judge 모델 설정이 없다/다르다: {sid}")
        digest = config_digest(config)
        if model_config_sha is not None and digest != model_config_sha:
            raise JudgeStatsError(f"Judge 모델 설정이 섞였다: {sid}")
        model_config_sha = digest
        if row.get("prompt_version") != judge.PROMPT_VERSION:
            raise JudgeStatsError(f"Judge 프롬프트 버전이 다르다: {sid}")
        if row.get("input_sha") != judge.input_sha(ep):
            raise JudgeStatsError(f"Judge 입력 지문이 다르다: {sid}")
        if type(row.get("repeat")) is not int or any(
            row.get(key) != value
            for key, value in (
                ("task_id", ep.task_id),
                ("repeat", ep.repeat),
                ("sut", ep.sut),
                ("cluster_id", ep.cluster_id),
            )
        ):
            raise JudgeStatsError(f"Judge 에피소드·묶음 대응이 다르다: {sid}")
        previous = latest.get(eid)
        if previous is not None and previous.get("status") == "ok":
            raise JudgeStatsError(f"완료 Judge 판정이 중복되거나 완료 뒤 이력이 있다: {sid}")
        status, grounded, claims = row.get("status"), row.get("grounded"), row.get("claims")
        if status not in {"ok", "aborted"} or (grounded is not None and type(grounded) is not bool):
            raise JudgeStatsError(f"Judge 상태·판정 형식이 다르다: {sid}")
        if grounded is not None:
            if status != "ok" or row.get("parse_failed") is True or not isinstance(claims, Mapping):
                raise JudgeStatsError(f"Judge 판정/상태가 모순이다: {sid}")
            if (
                set(claims) != set(judge.VERDICTS)
                or any(type(value) is not int or value < 0 for value in claims.values())
                or sum(claims.values()) > judge.MAX_CLAIMS
            ):
                raise JudgeStatsError(f"Judge 주장 개수 형식이 다르다: {sid}")
            if grounded != (claims["unsupported"] == 0):
                raise JudgeStatsError(f"Judge 주장·전체 판정이 모순이다: {sid}")
        elif claims is not None or (status == "ok" and row.get("parse_failed") is not True):
            raise JudgeStatsError(f"Judge 판정 누락 기록이 모순이다: {sid}")
        cost, attempts = row.get("cost"), row.get("attempts", 0)
        if (
            isinstance(cost, bool)
            or not isinstance(cost, int | float)
            or not math.isfinite(cost)
            or cost < 0
        ):
            raise JudgeStatsError(f"Judge 비용 형식이 다르다: {sid}")
        if type(attempts) is not int or attempts < 0:
            raise JudgeStatsError(f"Judge 시도 수 형식이 다르다: {sid}")
        info = meta.setdefault(
            sid,
            {
                "history_rows": 0,
                "aborted_rows": 0,
                "aborted_attempts": 0,
                "cost_usd_all_history": 0.0,
            },
        )
        info["history_rows"] += 1
        info["cost_usd_all_history"] += cost
        if status == "aborted":
            info["aborted_rows"] += 1
            info["aborted_attempts"] += attempts
        latest[eid] = row
    return latest, meta


def _view(record: Mapping[str, Any], success: bool) -> Mapping[str, Any]:
    # 원본 config와 config_fingerprint는 그대로다. 통계용 성공 값만 복제하며 원본은 바꾸지 않는다.
    return {**record, "outcome": {**record["outcome"], "success": success}}


def _rate(
    rows: Sequence[Mapping[str, Any]], resamples: int, seed: int, *, primary: bool = False
) -> dict[str, Any]:
    if not rows or not any(row["outcome"]["success"] is not None for row in rows):
        return {"available": False, "reason": "no_scored_episodes", "n_episodes": len(rows)}
    return {
        "available": True,
        **asdict(
            stats.success_rate(rows, resamples=resamples, seed=seed, include_incomplete=not primary)
        ),
    }


def _paired(
    a: Sequence[Mapping[str, Any]],
    baseline: Sequence[Mapping[str, Any]],
    resamples: int,
    seed: int,
) -> dict[str, Any]:
    a_scored = {str(row["task_id"]) for row in a if row["outcome"]["success"] is not None}
    b_scored = {str(row["task_id"]) for row in baseline if row["outcome"]["success"] is not None}
    if not a_scored & b_scored:
        return {
            "available": False,
            "reason": "no_scored_pairs",
            "n_tasks": 0,
            "n_unpaired_a": len(a_scored),
            "n_unpaired_b": len(b_scored),
        }
    return {
        "available": True,
        **asdict(
            stats.paired_difference(
                a, baseline, resamples=resamples, seed=seed, include_incomplete=True
            )
        ),
    }


def summarize(
    manifest: Manifest,
    journal: Sequence[Mapping[str, Any]],
    *,
    family: str,
    model_id: str,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> dict[str, Any]:
    if manifest.baseline not in {setting.id for setting in manifest.settings}:
        raise JudgeStatsError("summary manifest에 baseline 설정이 없다")
    if len(manifest.settings) != manifest.expected_settings:
        raise JudgeStatsError("summary manifest의 설정 수가 기대치와 다르다")
    inputs, preparation = prepare(manifest)
    expected = {
        judge.episode_id_of(row): (as_episode(row), str(row["setting_id"])) for row in inputs
    }
    latest, history = _history(journal, expected, family, model_id)
    views: dict[str, dict[str, list[Mapping[str, Any]]]] = {}
    reports: dict[str, Any] = {}
    for setting in manifest.settings:
        variant: dict[str, list[Mapping[str, Any]]] = {
            name: []
            for name in (
                "primary_success",
                "strict_success_fixed_all",
                "grounded_fixed_eligible",
                "grounded_conditional_verdict",
                "strict_success_conditional_verdict",
            )
        }
        claims = dict.fromkeys(judge.VERDICTS, 0)
        missing: Counter[str] = Counter()
        primary_counts: Counter[str] = Counter()
        valid_count = 0
        for record in setting.records:
            primary = record["outcome"]["success"]
            primary_counts[
                "unscored" if primary is None else "success" if primary else "failure"
            ] += 1
            prepared, _ = prepare_record(setting.id, record)
            verdict = latest.get(judge.episode_id_of(prepared)) if prepared is not None else None
            grounded = verdict.get("grounded") if verdict is not None else None
            valid = verdict is not None and verdict["status"] == "ok" and type(grounded) is bool
            strict = primary is True and valid and grounded is True
            variant["primary_success"].append(record)
            variant["strict_success_fixed_all"].append(_view(record, strict))
            if prepared is not None:
                variant["grounded_fixed_eligible"].append(_view(record, valid and grounded is True))
                if valid:
                    valid_count += 1
                    variant["grounded_conditional_verdict"].append(_view(record, grounded is True))
                    variant["strict_success_conditional_verdict"].append(_view(record, strict))
                    for kind in judge.VERDICTS:
                        claims[kind] += verdict["claims"][kind]  # type: ignore[index]
                else:
                    reason = (
                        "absent_row"
                        if verdict is None
                        else "aborted"
                        if verdict["status"] == "aborted"
                        else "parse_failed"
                    )
                    missing[reason] += 1
        views[setting.id] = variant
        code = metrics_v1.fact_support(setting.records)
        reports[setting.id] = {
            **preparation[setting.id],
            "primary_outcome_counts": dict(primary_counts),
            "valid_verdicts": valid_count,
            "missing_verdicts": sum(missing.values()),
            "missing_reasons": dict(sorted(missing.items())),
            "claim_counts": claims,
            "judge_history": history.get(
                setting.id,
                {
                    "history_rows": 0,
                    "aborted_rows": 0,
                    "aborted_attempts": 0,
                    "cost_usd_all_history": 0.0,
                },
            ),
            "code_faithfulness": code.get("faithfulness"),
            "code_fact_support": code,
            **{
                name: _rate(rows, resamples, seed, primary=name == "primary_success")
                for name, rows in variant.items()
            },
        }
    baseline = views[manifest.baseline]
    for sid, variant in views.items():
        reports[sid]["paired_a_minus_baseline"] = {
            name: _paired(rows, baseline[name], resamples, seed) for name, rows in variant.items()
        }
    return {
        "baseline": manifest.baseline,
        "judge_model_id": model_id,
        "judge_family": family,
        "prompt_version": judge.PROMPT_VERSION,
        "bootstrap": {"resamples": resamples, "seed": seed, "unit": "shared_cluster", "ci": 0.95},
        "settings": reports,
        "policies": {
            "primary_success": "기존 stats 규칙 유지: outcome.success None은 분모 제외, 별도 집계",
            "strict_success_fixed_all": "전체 원본 분모. primary 성공+유효 grounded 참이어야 성공; "
            "제외/누락/불채점은 0",
            "grounded_fixed_eligible": "기록과 답이 있는 eligible 전체 분모; 누락 판정은 0",
            "conditional_verdict": "유효 Judge 판정이 있는 행만 쓰는 보조값; 분모 감소를 명시",
            "grounded_pairs": "양쪽 eligible 공통 과제 비교; 조건부 쌍은 양쪽 유효 판정만",
            "cost": "원본 journal의 aborted 포함 모든 시도 비용 합계; logical 판정과 별도",
            "errors": "C 입력은 실제 오류 도구 본문/종류 포함; 기존 코드 사실 근거율은 그대로",
        },
    }


def _write_json(path: Path, report: Mapping[str, Any]) -> None:
    if path.exists():
        raise JudgeStatsError("이미 있는 출력에는 쓰지 않는다")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(dict(report), handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "summarize"):
        command = commands.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        if name == "summarize":
            command.add_argument("--judge", type=Path, required=True)
            command.add_argument("--family", required=True)
            command.add_argument("--model-id", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        if args.command == "prepare":
            inputs, counts = prepare(manifest)
            if args.out.exists():
                raise JudgeStatsError("이미 있는 입력에는 쓰지 않는다")
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("x", encoding="utf-8") as handle:
                for row in inputs:
                    handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            print(
                json.dumps({"rows": len(inputs), "settings": counts}, ensure_ascii=False, indent=2)
            )
        else:
            path = args.judge / judge.JUDGE_FILE if args.judge.is_dir() else args.judge
            report = summarize(
                manifest, _read_rows(path), family=args.family, model_id=args.model_id
            )
            _write_json(args.out, report)
            print(
                json.dumps(
                    {"settings": len(manifest.settings), "prompt_version": judge.PROMPT_VERSION}
                )
            )
    except (JudgeStatsError, stats.ResultsError, OSError) as exc:
        print(f"집계 오류: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
