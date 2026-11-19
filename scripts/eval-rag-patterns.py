#!/usr/bin/env python3
"""ADR-0035 experiment supervision; no credentials or raw answer text are printed.

The runner's dollar cap is checked at episode boundaries. This controller reserves
$0.15 for the last episode and stops the whole study on unobserved costs. It does
not claim to bound a provider's final invoice or recover the cost of a killed call.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs/rag-patterns-v1"
DATASET = ROOT / "eval/datasets/orbit-eval/v1"
CACHE_SOURCE = Path(os.environ.get("ORBIT_RAG_CACHE_SOURCE", str(ROOT / "runs/embed-cache")))
CACHE = RUNS / "embed-cache"
PATTERNS = ("agentic", "retrieve_once", "rewrite_retrieve_once")
STUDY_CAP = 1.0
LAST_EPISODE_RESERVE = 0.15
EMBED_CAP = 0.005
WATCHDOG_SECONDS = 600
ESTIMATE_TASKS = 8
CAPS = {"estimate": 0.025, "mail": 0.22, "smoke": 0.05}
CHILD: subprocess.Popen[Any] | None = None
ACTIVE_STAGE: dict[str, Any] | None = None
GIT = shutil.which("git")
PS = shutil.which("ps")
if GIT is None or PS is None:
    raise RuntimeError("git_and_ps_required")


class StudyInterrupted(RuntimeError):
    pass


def interrupt(signum: int, _frame: Any) -> None:
    raise StudyInterrupted(f"signal_{signum}")


def emit(event: str, **data: Any) -> None:
    print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def write_once(path: Path, data: Any) -> None:
    with path.open("x", encoding="utf-8") as out:
        json.dump(data, out, ensure_ascii=False, indent=2, sort_keys=True)
        out.write("\n")


def rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    # A concurrent writer may be in the middle of a UTF-8 character or JSON line.
    raw = path.read_bytes()
    if b"\n" not in raw:
        return []
    complete = raw.rsplit(b"\n", 1)[0]
    return [json.loads(line) for line in complete.splitlines() if line.strip()]


def observed_cost() -> tuple[float, int]:
    cost, unknown = 0.0, 0
    for path in RUNS.glob("*.jsonl"):
        for row in rows(path):
            value = row.get("usage", {}).get("cost_usd")
            if value is None:
                unknown += 1
            else:
                cost += float(value)
    for path in RUNS.glob("*.embedding.json"):
        value = json.loads(path.read_text())["cost_usd"]
        if value is None:
            unknown += 1
        else:
            cost += float(value)
    return cost, unknown


def require_observed() -> float:
    cost, unknown = observed_cost()
    if unknown:
        raise RuntimeError(f"cost_unobserved:{unknown}")
    return cost


def git_output(args: tuple[str, ...]) -> str:
    if args not in {("rev-parse", "HEAD"), ("status", "--porcelain")}:
        raise ValueError("unsupported_git_query")
    return subprocess.check_output(  # noqa: S603  installed Git, read-only allowlisted arguments
        [GIT, *args], cwd=ROOT, text=True
    ).strip()


def study_signature() -> dict[str, Any]:
    from orbit.evalrunner.task_source import load_dataset

    dataset = load_dataset(DATASET)
    mail = [t for t in dataset.tasks_for("dev") if "scenario:mail_fact" in t.tags]
    smoke_ids = set(dataset.manifest["smoke_task_ids"])
    smoke = [t for t in dataset.tasks_for("dev") if t.task_id in smoke_ids]
    hashes = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (
            Path(__file__),
            ROOT / "scripts/eval-rag-patterns.sh",
            DATASET / "manifest.json",
            RUNS / "model-price-20261119.json",
        )
    }
    return {
        "code_sha": git_output(("rev-parse", "HEAD")),
        "file_hashes": hashes,
        "mail_task_ids": sorted(t.task_id for t in mail),
        "smoke_task_ids": sorted(t.task_id for t in smoke),
        "mail_clusters": sorted({t.cluster_id for t in mail}),
        "patterns": PATTERNS,
        "seed": 0,
        "mail_repeats": 3,
        "smoke_repeats": 1,
        "stage_caps": CAPS,
        "embedding_cap": EMBED_CAP,
        "study_cap": STUDY_CAP,
        "reserve": LAST_EPISODE_RESERVE,
        "model_settings": {
            k: os.environ.get(k)
            for k in (
                "ORBIT_LLM_PROVIDER",
                "ORBIT_LLM_MODEL_ID",
                "ORBIT_LLM_REASONING_EFFORT",
                "ORBIT_LLM_PRICE_SOURCE",
                "ORBIT_LLM_PRICE_INPUT_PER_MTOK",
                "ORBIT_LLM_PRICE_CACHED_INPUT_PER_MTOK",
                "ORBIT_LLM_PRICE_OUTPUT_PER_MTOK",
            )
        },
    }


def verify_frozen_study() -> dict[str, Any]:
    signature = study_signature()
    path = RUNS / "frozen-study.json"
    # JSON round-trip normalizes tuples to lists for exact comparison on resume.
    signature = json.loads(json.dumps(signature, sort_keys=True))
    if path.exists():
        if json.loads(path.read_text()) != signature:
            raise RuntimeError("frozen_study_signature_changed")
    else:
        if any(RUNS.glob("*.jsonl")) or any(RUNS.glob("*.status.json")):
            raise RuntimeError("unfrozen_existing_results")
        write_once(path, signature)
    return signature


def validate_stage(
    pattern: str, kind: str, status: dict[str, Any], output: Path, embedding: Path
) -> None:
    frozen = json.loads((RUNS / "frozen-study.json").read_text())
    if (
        not status.get("ok")
        or status.get("unknown_costs")
        or status.get("code_sha") != frozen["code_sha"]
    ):
        raise RuntimeError("stage_not_verified_complete")
    if not output.exists() or not embedding.exists():
        raise RuntimeError("missing_stage_artifact")
    if not output.read_bytes().endswith(b"\n"):
        raise RuntimeError("partial_stage_result")
    data = rows(output)
    ids = frozen["smoke_task_ids"] if kind == "smoke" else frozen["mail_task_ids"]
    if kind == "estimate":
        from orbit.evalrunner.runner import select_tasks
        from orbit.evalrunner.task_source import load_dataset

        tasks = [t for t in load_dataset(DATASET).tasks_for("dev") if t.task_id in set(ids)]
        ids = [t.task_id for t in select_tasks(tasks, ESTIMATE_TASKS, 0)]
    repeats = 3 if kind == "mail" else 1
    expected = {(task_id, repeat) for task_id in ids for repeat in range(1, repeats + 1)}
    keys = [(row["task_id"], row["repeat"]) for row in data]
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise RuntimeError("stage_task_repeat_mismatch")
    for row in data:
        c = row["config"]
        if c.get("git_sha") != frozen["code_sha"] or c.get("git_dirty"):
            raise RuntimeError("stage_code_mismatch")
        if c.get("split") != "dev" or c.get("model_id") != "openai/gpt-6-luna":
            raise RuntimeError("stage_model_or_split_mismatch")
        if c.get("sut_config", {}).get("rag_pattern", "agentic") != pattern:
            raise RuntimeError("stage_pattern_mismatch")
        if row.get("usage", {}).get("cost_usd") is None:
            raise RuntimeError("stage_cost_unobserved")
    if json.loads(embedding.read_text()).get("cost_usd") is None:
        raise RuntimeError("embedding_cost_unobserved")
    if status.get("output_sha256") != hashlib.sha256(output.read_bytes()).hexdigest():
        raise RuntimeError("stage_output_hash_changed")
    if status.get("embedding_sha256") != hashlib.sha256(embedding.read_bytes()).hexdigest():
        raise RuntimeError("stage_embedding_hash_changed")


def other_evaluations() -> list[int]:
    snapshot = subprocess.check_output(  # noqa: S603  installed ps, fixed read-only arguments
        [PS, "-axo", "pid=,command="], text=True
    )
    matches = []
    for line in snapshot.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if " -m orbit.evalrunner run " in cmd or " -m orbit.evalrunner.retrieval " in cmd:
            matches.append(int(pid))
    return matches


def command(pattern: str, kind: str, cap: float) -> tuple[list[str], Path, Path]:
    name = f"{kind}-{pattern}"
    output = RUNS / f"{name}.jsonl"
    embedding = RUNS / f"{name}.embedding.json"
    cmd = [
        str(ROOT / "backend/.venv/bin/python"),
        "-m",
        "orbit.evalrunner",
        "run",
        "--dataset",
        str(DATASET),
        "--split",
        "dev",
        "--sut",
        "runflow_v0",
        "--pattern",
        "react",
        "--rag-pattern",
        pattern,
        "--subset",
        "smoke" if kind == "smoke" else "mail-content",
        "--index",
        "on",
        "--embedder",
        "openai",
        "--embed-cache-dir",
        str(CACHE),
        "--max-embed-cost-usd",
        str(EMBED_CAP),
        "--embedding-stats-out",
        str(embedding),
        "--price-per-mtok",
        "0.02",
        "--seed",
        "0",
        "--repeats",
        "1" if kind == "smoke" else "3",
        "--max-steps",
        "50",
        "--max-episode-tokens",
        "200000",
        "--max-seconds",
        "120",
        "--max-cost-usd",
        f"{cap:.8f}",
        "--prompt-version",
        "runflow_v0",
        "--experiment-id",
        f"rag-v1-{name}",
        "--live-llm",
        "--out",
        str(output),
    ]
    if kind == "estimate":
        cmd += ["--estimate", str(ESTIMATE_TASKS)]
        output = output.with_name(output.stem + ".estimate.jsonl")
    return cmd, output, embedding


def terminate_own_group(proc: subprocess.Popen[Any]) -> None:
    if proc.poll() is not None:
        return
    os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def run_stage(pattern: str, kind: str) -> dict[str, Any]:
    global CHILD, ACTIVE_STAGE
    if (RUNS / "STOPPED.json").exists():
        raise RuntimeError("study_previously_stopped")
    if other_evaluations():
        raise RuntimeError("other_evaluation_running")
    spent = require_observed()
    cap = min(CAPS[kind], STUDY_CAP - spent - EMBED_CAP - LAST_EPISODE_RESERVE)
    if cap <= 0:
        raise RuntimeError("study_budget_exhausted")
    cmd, output, embedding = command(pattern, kind, cap)
    status_path = RUNS / f"{kind}-{pattern}.status.json"
    if status_path.exists():
        status = json.loads(status_path.read_text())
        validate_stage(pattern, kind, status, output, embedding)
        emit("already_complete", pattern=pattern, kind=kind)
        return status
    if output.exists() or embedding.exists():
        raise RuntimeError("stage_has_unreconciled_output")
    metadata = {
        "pattern": pattern,
        "kind": kind,
        "llm_cap_usd": cap,
        "embedding_cap_usd": EMBED_CAP,
        "study_cap_usd": STUDY_CAP,
        "cost_before_usd": spent,
        "last_episode_reserve_usd": LAST_EPISODE_RESERVE,
        "command": cmd,
        "code_sha": git_output(("rev-parse", "HEAD")),
    }
    write_once(RUNS / f"{kind}-{pattern}.command.json", metadata)
    ACTIVE_STAGE = {
        "pattern": pattern,
        "kind": kind,
        "metadata": metadata,
        "started": time.monotonic(),
    }
    emit("stage_start", **{k: v for k, v in metadata.items() if k != "command"})
    started = time.monotonic()
    last_change = started
    last_size = -1
    reason = None
    log = RUNS / f"{kind}-{pattern}.log"
    with log.open("x", encoding="utf-8") as log_out:
        proc = subprocess.Popen(  # noqa: S603  absolute Python + locally built fixed-enum argv, no shell
            cmd,
            cwd=ROOT / "backend",
            stdout=log_out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        CHILD = proc
        try:
            while proc.poll() is None:
                size = output.stat().st_size if output.exists() else 0
                if size != last_size:
                    last_change, last_size = time.monotonic(), size
                    cost, unknown = observed_cost()
                    emit(
                        "progress",
                        pattern=pattern,
                        kind=kind,
                        episodes=len(rows(output)),
                        study_cost_usd=cost,
                        unknown=unknown,
                    )
                    if unknown:
                        reason = "cost_unobserved"
                        break
                    if cost + EMBED_CAP >= STUDY_CAP - LAST_EPISODE_RESERVE:
                        reason = "study_cost_guard"
                        break
                if time.monotonic() - last_change > WATCHDOG_SECONDS:
                    reason = "no_progress_600_seconds"
                    break
                time.sleep(1)
        finally:
            if proc.poll() is None:
                terminate_own_group(proc)
    result_rows = rows(output)
    if output.exists() and not output.read_bytes().endswith(b"\n"):
        reason = reason or "partial_result_line"
    expected = ESTIMATE_TASKS if kind == "estimate" else 21 if kind == "smoke" else 63
    cost, unknown = observed_cost()
    status = {
        **metadata,
        "returncode": proc.returncode,
        "reason": reason,
        "episodes": len(result_rows),
        "expected_episodes": expected,
        "duration_seconds": time.monotonic() - started,
        "study_cost_usd": cost,
        "unknown_costs": unknown,
        "ok": reason is None
        and proc.returncode == 0
        and len(result_rows) == expected
        and unknown == 0
        and embedding.exists()
        and cost < STUDY_CAP,
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest()
        if output.exists()
        else None,
        "embedding_sha256": hashlib.sha256(embedding.read_bytes()).hexdigest()
        if embedding.exists()
        else None,
    }
    if status["ok"]:
        validate_stage(pattern, kind, status, output, embedding)
    write_once(status_path, status)
    CHILD, ACTIVE_STAGE = None, None
    emit("stage_finish", **{k: v for k, v in status.items() if k != "command"})
    if not status["ok"]:
        write_once(
            RUNS / "STOPPED.json",
            {
                "stage": f"{kind}-{pattern}",
                "reason": reason or "incomplete_or_failed",
                "cost_observed_usd": cost,
                "in_flight_cost_may_be_unknown": reason is not None,
            },
        )
        raise RuntimeError("stage_stopped")
    return status


def estimate_gate() -> dict[str, Any]:
    if (RUNS / "STOPPED.json").exists():
        raise RuntimeError("study_previously_stopped")
    estimates = []
    for pattern in PATTERNS:
        path = RUNS / f"estimate-{pattern}.estimate.jsonl"
        status_path = RUNS / f"estimate-{pattern}.status.json"
        embedding = RUNS / f"estimate-{pattern}.embedding.json"
        if not status_path.exists():
            raise RuntimeError("missing_estimate_status")
        validate_stage(pattern, "estimate", json.loads(status_path.read_text()), path, embedding)
        data = rows(path)
        if len(data) != ESTIMATE_TASKS:
            raise RuntimeError("missing_complete_estimates")
        values = [float(r["usage"]["cost_usd"]) for r in data]
        mail = sum(values) / len(values) * 63
        # Same mail-task sample is only a rough smoke forecast; report this limit.
        smoke = sum(values) / len(values) * 21
        estimates.append(
            {
                "pattern": pattern,
                "sampled_episodes": len(data),
                "mail_projected_usd": mail,
                "smoke_proxy_projected_usd": smoke,
            }
        )
    spent = require_observed()
    remaining = 0.0
    for e in estimates:
        for kind, key in (("mail", "mail_projected_usd"), ("smoke", "smoke_proxy_projected_usd")):
            status_path = RUNS / f"{kind}-{e['pattern']}.status.json"
            if status_path.exists():
                validate_stage(
                    e["pattern"],
                    kind,
                    json.loads(status_path.read_text()),
                    RUNS / f"{kind}-{e['pattern']}.jsonl",
                    RUNS / f"{kind}-{e['pattern']}.embedding.json",
                )
            else:
                remaining += e[key] + EMBED_CAP
    projected = spent + remaining
    fits_stages = all(
        e["mail_projected_usd"] <= CAPS["mail"] and e["smoke_proxy_projected_usd"] <= CAPS["smoke"]
        for e in estimates
    )
    return {
        "estimates": estimates,
        "spent_estimates_usd": spent,
        "projected_study_usd": projected,
        "gate_usd": STUDY_CAP - LAST_EPISODE_RESERVE,
        "allowed": fits_stages and projected <= STUDY_CAP - LAST_EPISODE_RESERVE,
        "smoke_estimate_limit": (
            "Mail estimate sample is a smoke proxy; "
            "stage caps and observed-cost supervision remain active."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("plan", "estimate", "run"))
    args = parser.parse_args()
    if args.mode == "plan":
        emit(
            "plan",
            patterns=PATTERNS,
            mail_tasks=21,
            mail_clusters=17,
            mail_repeats=3,
            smoke_tasks=21,
            smoke_repeats=1,
            subset_overlap=7,
            study_cap_usd=STUDY_CAP,
            reserve_usd=LAST_EPISODE_RESERVE,
            live_calls=0,
        )
        return
    RUNS.mkdir(parents=True, exist_ok=True)
    dirty = git_output(("status", "--porcelain"))
    if dirty:
        raise RuntimeError("tracked_worktree_changes_must_be_committed_before_paid_run")
    with Path("/private/tmp/orbit-evaluation-runflow.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if other_evaluations():
            raise RuntimeError("other_evaluation_running")
        verify_frozen_study()
        if not CACHE.exists():
            shutil.copytree(CACHE_SOURCE, CACHE)
        if args.mode == "estimate":
            for pattern in PATTERNS:
                run_stage(pattern, "estimate")
            gate = estimate_gate()
            write_once(RUNS / "estimate-gate.json", gate)
            emit("estimate_gate", **gate)
        else:
            gate = estimate_gate()
            if not gate["allowed"]:
                emit("estimate_gate_rejected", **gate)
                raise SystemExit(3)
            for kind in ("mail", "smoke"):
                for pattern in PATTERNS:
                    run_stage(pattern, kind)
            emit("study_complete", study_cost_usd=require_observed())


def record_interruption(reason: str) -> None:
    if CHILD is not None:
        terminate_own_group(CHILD)
    if ACTIVE_STAGE is None:
        return
    stage = ACTIVE_STAGE
    path = RUNS / f"{stage['kind']}-{stage['pattern']}.status.json"
    if not path.exists():
        write_once(
            path,
            {
                **stage["metadata"],
                "ok": False,
                "reason": reason,
                "in_flight_cost_may_be_unknown": True,
            },
        )
    stopped = RUNS / "STOPPED.json"
    if not stopped.exists():
        write_once(
            stopped,
            {
                "stage": f"{stage['kind']}-{stage['pattern']}",
                "reason": reason,
                "in_flight_cost_may_be_unknown": True,
            },
        )


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        main()
    except (RuntimeError, BlockingIOError, KeyboardInterrupt) as exc:
        record_interruption(str(exc) or type(exc).__name__)
        emit("stopped", reason=str(exc))
        raise SystemExit(3) from None
    except Exception as exc:
        record_interruption(type(exc).__name__)
        emit("stopped", reason=type(exc).__name__)
        raise SystemExit(3) from None
