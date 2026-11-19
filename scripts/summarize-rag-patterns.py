#!/usr/bin/env python3
"""Read-only offline aggregation of ADR-0035 outputs; never calls LLMs or DBs."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from orbit.evalrunner import stats

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs/rag-patterns-v1"
PATTERNS = ("agentic", "retrieve_once", "rewrite_retrieve_once")
LABELS = {
    "agentic": "R0 · 도구 호출형",
    "retrieve_once": "R1 · Retrieve-once",
    "rewrite_retrieve_once": "R2 · Rewrite → Retrieve-once",
}


def write_once(path: Path, data: Any) -> None:
    if path.exists():
        if json.loads(path.read_text()) == data:
            return
        raise RuntimeError(f"existing_aggregate_differs:{path.name}")
    with path.open("x", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")


def analyze(path: Path, baseline: Path) -> dict[str, Any]:
    cmd = [
        str(ROOT / "backend/.venv/bin/python"),
        "-m",
        "orbit.evalrunner",
        "stats",
        str(path),
        "--compare",
        str(baseline),
        "--resamples",
        "10000",
        "--seed",
        "0",
        "--reject-model-mismatch",
    ]
    text = subprocess.check_output(cmd, cwd=ROOT / "backend", text=True)  # noqa: S603  local Python + offline stats argv
    return json.loads(text)


def diagnostics(data: list[stats.Record]) -> dict[str, Any]:
    indexed, initial, errors, zero, skipped, attempted = 0, 0, 0, 0, 0, 0
    rewrite, fallback, rewrite_errors, truncated = 0, 0, Counter(), 0
    for row in data:
        calls = (row.get("trace") or {}).get("tool_calls", [])
        saw = False
        for c in calls:
            if c["name"] != "search_indexed_mail":
                continue
            indexed += 1
            if c.get("origin") == "rag_initial":
                initial += 1
            if c.get("is_error"):
                errors += 1
                continue
            saw = True
            attempted += 1
            try:
                result = json.loads(c.get("result") or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(result, dict) and result.get("results") == []:
                zero += 1
        skipped += not saw
        for step in (row.get("trace") or {}).get("pattern_steps", []):
            if step.get("kind") == "rag_rewrite":
                rewrite += 1
                fallback += bool(step.get("fallback"))
                if step.get("error_kind"):
                    rewrite_errors[step["error_kind"]] += 1
            if step.get("kind") == "rag_query_truncated":
                truncated += 1
    return {
        "indexed_tool_records": indexed,
        "server_initial_records": initial,
        "index_errors": errors,
        "successful_index_calls": attempted,
        "empty_results": zero,
        "episodes_without_successful_index_call": skipped,
        "rewrite_records": rewrite,
        "rewrite_fallback": fallback,
        "rewrite_errors": dict(rewrite_errors),
        "query_truncated": truncated,
    }


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def rate(value: dict[str, Any]) -> str:
    return f"{percent(value['rate'])} [{value['ci_low'] * 100:.1f}, {value['ci_high'] * 100:.1f}]"


def difference(value: dict[str, Any]) -> str:
    return (
        f"{value['difference'] * 100:+.1f} pp "
        f"[{value['ci_low'] * 100:+.1f}, {value['ci_high'] * 100:+.1f}]"
    )


def mdtable(headers: list[str], values: list[list[str]]) -> str:
    return "\n".join(
        [
            "| " + " | ".join(headers) + " |",
            "| " + " | ".join(["---"] * len(headers)) + " |",
            *["| " + " | ".join(row) + " |" for row in values],
        ]
    )


def main() -> None:
    if (RUNS / "STOPPED.json").exists():
        raise RuntimeError("stopped_study_requires_explicit_partial_report")
    frozen = json.loads((RUNS / "frozen-study.json").read_text())
    # Import the supervisor for the same artifact and task-set verification.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "rag_supervisor", ROOT / "scripts/eval-rag-patterns.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("supervisor_module_missing")
    supervisor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(supervisor)
    report: dict[str, Any] = {
        "code_sha": frozen["code_sha"],
        "model": "openai/gpt-6-luna",
        "agent_pattern": "react",
        "reasoning_effort": "none",
        "split": "dev",
        "mail": {},
        "smoke": {},
        "cost": {},
        "sources": {},
    }
    for kind in ("estimate", "mail", "smoke"):
        for pattern in PATTERNS:
            output = RUNS / (
                f"estimate-{pattern}.estimate.jsonl"
                if kind == "estimate"
                else f"{kind}-{pattern}.jsonl"
            )
            status_path = RUNS / f"{kind}-{pattern}.status.json"
            embedding = RUNS / f"{kind}-{pattern}.embedding.json"
            status = json.loads(status_path.read_text())
            supervisor.validate_stage(pattern, kind, status, output, embedding)
            data = stats.load_results(output, reject_model_mismatch=True)
            report["cost"][f"{kind}-{pattern}"] = {
                "llm_usd": sum(float(r["usage"]["cost_usd"]) for r in data),
                "embedding_usd": json.loads(embedding.read_text())["cost_usd"],
                "episodes": len(data),
            }
            for p in (output, status_path, embedding):
                report["sources"][p.name] = hashlib.sha256(p.read_bytes()).hexdigest()
            if kind == "estimate":
                continue
            result = analyze(output, RUNS / f"{kind}-agentic.jsonl")
            target = RUNS / f"{kind}-{pattern}.stats.json"
            write_once(target, result)
            report["sources"][target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
            report[kind][pattern] = {
                "stats": result,
                "diagnostics": diagnostics(data),
                "config_fingerprint": data[0]["config_fingerprint"],
                "sut_config": data[0]["config"].get("sut_config"),
                "file": output.name,
                "success_count": sum(r["outcome"]["success"] is True for r in data),
            }
    pair = analyze(RUNS / "mail-rewrite_retrieve_once.jsonl", RUNS / "mail-retrieve_once.jsonl")
    write_once(RUNS / "rewrite-vs-raw.stats.json", pair)
    report["rewrite_vs_raw"] = pair["paired_difference_a_minus_b"]
    report["cost_total_usd"] = sum(
        c["llm_usd"] + c["embedding_usd"] for c in report["cost"].values()
    )
    if report["cost_total_usd"] >= 1.0:
        raise RuntimeError("study_cost_cap_exceeded")
    if not all(
        not x["stats"]["success_rate"]["truncated"]
        for k in ("mail", "smoke")
        for x in report[k].values()
    ):
        raise RuntimeError("incomplete_rounds")
    write_once(RUNS / "report.json", report)
    body = [
        f"실행 코드 SHA: `{report['code_sha']}`. "
        "모든 설정을 같은 커밋에서 실행했고 `git_dirty=false`다. test 사용 0회.\n"
    ]
    body.append("### 주 비교: 메일 dev 21과제 × 3회 · 17 clusters\n")
    values = []
    for p in PATTERNS:
        r = report["mail"][p]
        s = r["stats"]
        e = s["efficiency"]
        values.append(
            [
                LABELS[p],
                f"{r['success_count']}/63",
                rate(s["success_rate"]),
                "기준" if p == "agentic" else difference(s["paired_difference_a_minus_b"]),
                percent(s["pass_k"]["pass_k"]),
                f"${e['cost_total_usd']:.8f}",
                f"{e['latency_ms_p50'] / 1000:.2f} / {e['latency_ms_p95'] / 1000:.2f}초",
            ]
        )
    body.append(
        mdtable(
            [
                "RAG 패턴",
                "성공/episodes",
                "성공률 [95% CI]",
                "R0 대비 짝 차이 [95% CI]",
                "pass³",
                "본 실행 LLM 비용",
                "지연 p50/p95",
            ],
            values,
        )
    )
    body.append(f"\n**R2−R1(질의 재작성의 추가 효과):** {difference(report['rewrite_vs_raw'])}.\n")
    improvements = [
        LABELS[p]
        for p in PATTERNS[1:]
        if report["mail"][p]["stats"]["paired_difference_a_minus_b"]["ci_low"] > 0
    ]
    body.append(
        "**판단:** "
        + (
            "양의 paired CI를 보인 후보: "
            + ", ".join(improvements)
            + ". 기준 변경은 개발자 결정 필요."
            if improvements
            else (
                "기준 대비 양의 paired CI를 보인 후보가 없어 기준 설정을 유지한다. "
                "CI가 0을 포함하는 비교를 동등성 입증으로 해석하지 않는다."
            )
        )
        + "\n"
    )
    body.append("### 검색·재작성 동작 관찰\n")
    body.append(
        mdtable(
            [
                "패턴",
                "색인 도구 기록",
                "서버 초기 검색",
                "검색 오류",
                "0건 결과",
                "재작성 기록 / fallback",
                "질의 절단",
            ],
            [
                [
                    LABELS[p],
                    str(report["mail"][p]["diagnostics"]["indexed_tool_records"]),
                    str(report["mail"][p]["diagnostics"]["server_initial_records"]),
                    str(report["mail"][p]["diagnostics"]["index_errors"]),
                    str(report["mail"][p]["diagnostics"]["empty_results"]),
                    str(report["mail"][p]["diagnostics"]["rewrite_records"])
                    + " / "
                    + str(report["mail"][p]["diagnostics"]["rewrite_fallback"]),
                    str(report["mail"][p]["diagnostics"]["query_truncated"]),
                ]
                for p in PATTERNS
            ],
        )
    )
    body.append(
        "\n검색 횟수는 턴별 기록 합계로 episode 수와 다르다. "
        "오류 거절도 도구 기록에 포함되고 실제 성공 검색 수와는 구분한다. "
        "이 지표로 정답 근거 Recall이나 환각률을 추정하지 않는다.\n"
    )
    body.append("### 스모크: 21과제 × 1회 · 별도 집계\n")
    body.append(
        mdtable(
            ["패턴", "성공/21", "성공률 [95% CI]", "R0 대비 짝 차이 [95% CI]"],
            [
                [
                    LABELS[p],
                    f"{report['smoke'][p]['success_count']}/21",
                    rate(report["smoke"][p]["stats"]["success_rate"]),
                    "기준"
                    if p == "agentic"
                    else difference(report["smoke"][p]["stats"]["paired_difference_a_minus_b"]),
                ]
                for p in PATTERNS
            ],
        )
    )
    body.append(
        "\n주 비교와 겹치는 7과제는 따로 표기했으며 이 표를 합산해 독립 표본을 늘리지 않았다. "
        "스모크는 채택 근거가 아닌 기능 퇴행 관찰용이다.\n"
    )
    body.append("### 실행별 비용 · 사전 추정 포함\n")
    body.append(
        mdtable(
            ["실행", "episodes", "LLM USD", "임베딩 USD"],
            [
                [k, str(c["episodes"]), f"{c['llm_usd']:.8f}", f"{c['embedding_usd']:.8f}"]
                for k, c in report["cost"].items()
            ],
        )
    )
    body.append(
        f"\n**합계: ${report['cost_total_usd']:.8f}**. 비용 미상 0건. 단가는 "
        "[OpenRouter models API](https://openrouter.ai/api/v1/models)의 "
        "2026-11-19 스냅샷과 같은 표준 단가로 설정했다"
        "(입력/캐시/출력 1M tokens당 $0.10/$0.01/$0.50). "
        "임베딩은 $0.02/1M tokens다. 실제 청구액 대조는 아니다.\n"
    )
    body.append("### 설정 지문과 원본 출처\n")
    body.append(
        mdtable(
            ["집합", "패턴", "설정 지문", "결과 원본"],
            [
                [
                    kind,
                    LABELS[p],
                    f"`{r['config_fingerprint']}`",
                    f"`runs/rag-patterns-v1/{r['file']}`",
                ]
                for kind in ("mail", "smoke")
                for p, r in report[kind].items()
            ],
        )
    )
    body.append(
        "\n각 `*.stats.json`에 기존 보조 지표 전 열, "
        "`report.json`에 검색/fallback 진단과 비용·해시를 보존했다. "
        "`frozen-study.json`에는 선정 task·cluster·코드·단가·스크립트·manifest 해시가 있다. "
        "원문과 개별 판정은 git 제외다.\n"
    )
    body.append(
        mdtable(
            ["파일 · RUNS 기준", "SHA-256"],
            [[f"`{name}`", f"`{digest}`"] for name, digest in report["sources"].items()],
        )
    )
    target = ROOT / "docs/eval/rag-patterns-v1.md"
    text = target.read_text()
    before, rest = text.split("<!-- RAG_RESULTS_START -->", 1)
    _, after = rest.split("<!-- RAG_RESULTS_END -->", 1)
    target.write_text(
        before
        + "<!-- RAG_RESULTS_START -->\n\n"
        + "\n\n".join(body)
        + "\n\n<!-- RAG_RESULTS_END -->"
        + after
    )
    print(
        json.dumps(
            {
                "summary": "report.json",
                "cost_total_usd": report["cost_total_usd"],
                "primary_success": {
                    p: report["mail"][p]["stats"]["success_rate"]["rate"] for p in PATTERNS
                },
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
