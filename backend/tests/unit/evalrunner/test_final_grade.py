"""Agent 최종 채점기(ADR-0040) 테스트. 외부 호출 없이 합성 데이터와 가짜 응답만 쓴다."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from orbit.evalrunner import cli
from orbit.evalrunner import final_grade as fg
from orbit.evalrunner.judge import judge_models
from orbit.evalrunner.rag_arch import jev
from orbit.evalrunner.rag_arch.correctness import JUDGED_FILE, Fact
from orbit.evalrunner.runner import SealedSplitError
from orbit.llm.types import LLMRequest, LLMResponse, LLMUsage

from .conftest import DATASET_DIR, good_script

FP = "fp"
FACT = Fact("f1", "회의 장소", ("별관",))
TASK = fg.GradeTask("t1", "어디서 해?", (FACT,), (FACT,), None, None)
EVENT = {"calendar_id": "c1", "event_id": "e1"}
KIND_TASK = fg.GradeTask("t1", "어디서 해?", (FACT,), (FACT,), "event_time", EVENT)


def row(
    *,
    success: bool | None = False,
    ftype: str | None = "wrong_answer",
    bad: tuple[str, ...] = ("answer_facts",),
    recall: float | None = 0.0,
    text: str = "합성 답",
    ids: dict[str, Any] | None = None,
    task_id: str = "t1",
    repeat: int = 0,
    split: str = "dev",
) -> dict[str, Any]:
    checks: dict[str, Any] = {c: False for c in bad}
    return {
        "task_id": task_id,
        "repeat": repeat,
        "config": {"split": split},
        "outcome": {"success": success, "failure_type": ftype, "detail": None},
        "checks": checks,
        "text_fact_recall": recall,
        "answer_ids": ids,
        "trace": {"final_text": text},
    }


def item(e: fg.Entry) -> str:
    assert e.episode
    return e.episode.item_id


def jev_row(entry: fg.Entry, prob: float) -> dict[str, Any]:
    assert entry.task and entry.episode
    body = jev.build_body(entry.task, entry.episode, jev.V4)
    return {
        "item_id": entry.episode.item_id,
        "prompt_version": jev.V4,
        "status": "ok",
        "input_sha": jev.body_sha(body),
        "probs": {"f1": prob, jev.NO_EXTRA_KEY: 1.0},
    }


def grade_one(
    r: dict[str, Any], task: fg.GradeTask, prob: float | None, gem: bool | None = None
) -> tuple[fg.Entry, dict[str, Any]]:
    (e,) = fg.plan([r], {task.task_id: task}, "res")
    jevs = {item(e): jev_row(e, prob)} if prob is not None and e.episode else {}
    g = {item(e): gem} if gem is not None and e.episode else {}
    return e, fg.final_row(e, jevs, g, FP)


def test_사실_없는_과제는_코드_판정() -> None:
    task = fg.GradeTask("t1", "q", (), (), None, None)
    e, out = grade_one(row(), task, None)
    assert not e.target and out["grade"]["rule"] == "code"
    assert out["outcome"] == row()["outcome"]


def test_코드_성공_Jev_정답은_jev_성공() -> None:
    r = row(success=True, ftype=None, bad=(), recall=1.0)
    e, out = grade_one(r, TASK, 0.9)
    assert e.target and fg.gemini_entries([e], {item(e): jev_row(e, 0.9)}) == []
    assert out["outcome"]["success"] is True and out["outcome"]["failure_type"] is None
    assert out["grade"] == {
        "rule": "jev", "code_success": True, "code_failure_type": None,
        "jev_min_prob": 0.9, "gemini_correct": None, "grader_fingerprint": FP,
    }  # fmt: skip


def test_문자열만_실패하고_Jev_정답이면_Gemini가_최종() -> None:
    e, out = grade_one(row(), TASK, 0.8, gem=False)  # 코드 오답 != Jev 정답 (A)
    assert fg.gemini_entries([e], {item(e): jev_row(e, 0.8)}) == [e]
    assert out["grade"]["rule"] == "gemini"
    assert out["outcome"]["success"] is False and out["outcome"]["failure_type"] == "wrong_answer"
    _, out = grade_one(row(), TASK, 0.8, gem=True)
    assert out["outcome"]["success"] is True and out["outcome"]["failure_type"] is None
    _, out = grade_one(row(), TASK, 0.8, gem=None)  # Gemini 판정 없음
    assert out["outcome"]["success"] is None


def test_경계_확률은_Gemini_대상() -> None:
    r = row(success=True, ftype=None, bad=(), recall=1.0)
    e, out = grade_one(r, TASK, 0.3, gem=False)  # Jev 정답(0.3>=0.25)이고 코드도 정답이지만 B
    assert fg.gemini_entries([e], {item(e): jev_row(e, 0.3)}) == [e]
    assert out["outcome"]["success"] is False
    assert fg.needs_gemini(True, 0.6) is False and fg.needs_gemini(True, 0.1) is True


def test_상태_변화_실패는_Jev_대상이_아니다() -> None:
    e, out = grade_one(
        row(ftype="wrong_state_change", bad=("answer_facts", "state_changes")), TASK, None
    )
    assert not e.target and fg.jev_items([e]) == []
    assert out["grade"]["rule"] == "code" and out["outcome"]["failure_type"] == "wrong_state_change"


def test_answer_kind_event_time_일치는_대상_오답이면_kind_유지() -> None:
    kw = dict(ftype="answer_kind", bad=("structured_answer",), recall=1.0)
    ids = {"kind": "event", "event": EVENT}
    e, out = grade_one(row(ids=ids, **kw), KIND_TASK, 0.9)  # type: ignore[arg-type]
    assert e.target and out["outcome"]["success"] is True and out["grade"]["rule"] == "jev"
    _, out = grade_one(row(ids=ids, **kw), KIND_TASK, 0.05, gem=False)  # type: ignore[arg-type]
    assert out["outcome"]["success"] is False and out["outcome"]["failure_type"] == "answer_kind"
    other = {"kind": "event", "event": {**EVENT, "event_id": "e2"}}
    e, _ = grade_one(row(ids=other, **kw), KIND_TASK, None)  # type: ignore[arg-type]
    assert not e.target  # 다른 일정은 실제 실패


def test_event_대_event_set은_코드_실패_유지() -> None:
    ids = {"kind": "event_set", "events": [EVENT]}
    r = row(ftype="answer_kind", bad=("structured_answer",), recall=1.0, ids=ids)
    e, out = grade_one(r, KIND_TASK, None)
    assert not e.target and out["outcome"]["failure_type"] == "answer_kind"
    assert out["grade"]["rule"] == "code"


def test_설명_중복_과제만_제목을_붙인다() -> None:
    dup = [
        {"fact_id": "a", "description": "일정 제목", "any_of": ["회의"]},
        {"fact_id": "b", "description": "일정 시작 시각", "any_of": ["10시"]},
        {"fact_id": "c", "description": "일정 제목", "any_of": ["점심"]},
        {"fact_id": "d", "description": "일정 시작 시각", "any_of": ["12시"]},
    ]
    assert [f.description for f in fg.context_facts(dup)] == [
        "일정 제목", "'회의' 일정의 시작 시각", "일정 제목", "'점심' 일정의 시작 시각",
    ]  # fmt: skip
    single = dup[:2]
    assert [f.description for f in fg.context_facts(single)] == ["일정 제목", "일정 시작 시각"]
    assert (
        fg.question_of(["a"]) == "a"
        and fg.question_of(["a", "b"]) == "1번째 요청: a\n2번째 요청: b"
    )


def test_빈_답은_판정_null() -> None:
    e, out = grade_one(row(text=""), TASK, None)
    assert e.empty and fg.jev_items([e]) == [] and out["outcome"]["success"] is None


# ---------------------------------------------------------------- CLI

ARCH_FACT_ROWS = [
    row(task_id="t-002", repeat=0, recall=0.0),  # 문자열 실패 -> Jev 정답 -> Gemini
    row(task_id="t-002", repeat=1, success=True, ftype=None, bad=(), recall=1.0),
    row(task_id="t-005", ftype="wrong_state_change", bad=("state_changes",)),
]


def run_args(tmp_path: Path, *extra: str) -> list[str]:
    script = tmp_path / "s.json"
    script.write_text("{}", encoding="utf-8")
    return [
        "run", "--dataset", str(DATASET_DIR), "--split", "dev", "--sut", "noop",
        "--out", str(tmp_path / "o.jsonl"), "--llm-script", str(script), *extra,
    ]  # fmt: skip


def test_llm_채점인데_상한이_없으면_파일_없이_거부(tmp_path: Path) -> None:
    assert cli.main(run_args(tmp_path, "--grade", "llm")) == 2
    assert not (tmp_path / "o.jsonl").exists()


def test_llm_채점인데_키가_없으면_거부(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in fg.KEYS:
        monkeypatch.delenv(k, raising=False)
    args = run_args(tmp_path, "--grade", "llm", "--max-grade-cost-usd", "1")
    assert cli.main(args) == 2
    assert not (tmp_path / "o.jsonl").exists()


def test_스크립트_실행은_기본으로_채점하지_않는다(tmp_path: Path) -> None:
    assert cli.main(run_args(tmp_path)) == 0
    assert not (tmp_path / "o.graded.jsonl").exists()


class FakeGemini:
    n = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        FakeGemini.n += 1
        model = judge_models(str(fg.GEMINI_CONFIG), ["google"])["google"]
        user = request.messages[-1].content or ""
        ids = re.findall(r'<fact id="([^"]+)"', user)
        text = json.dumps({"facts": [{"fact_id": i, "match": True} for i in ids], "correct": True})
        return LLMResponse(
            text=text, tool_calls=(), finish_reason="stop", usage=LLMUsage(1000, 100),
            cost_usd=None, latency_ms=1, model_id=model.model_id, provider=None,
        )  # fmt: skip


JEV_CALLS: list[int] = []
FAIL_FIRST = [False]


def jev_handler(req: httpx.Request) -> httpx.Response:
    JEV_CALLS.append(1)
    if FAIL_FIRST[0] and len(JEV_CALLS) == 1:
        return httpx.Response(400, json={"error": {"type": "bad_request"}})
    body = json.loads(req.content)
    answers = [
        {"type": "predicate", "name": q["name"], "probability": 0.9 if q["name"] != "x" else 0.05}
        for q in body["questions"]
    ]
    return httpx.Response(
        200,
        json={
            "answers": answers, "model": jev.JEV_MODEL, "usage": {"input_tokens": 100},
            "provider_metadata": {"gateway": {"cost": "0.00001"}},
        },
    )  # fmt: skip


def test_grade_명령_끝까지(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(jev.KEY_ENV, "test-key")
    monkeypatch.setenv("ORBIT_OPENROUTER_API_KEY", "test-key")
    res = tmp_path / "r.jsonl"
    res.write_text("".join(json.dumps(r) + "\n" for r in ARCH_FACT_ROWS), encoding="utf-8")
    base = ["grade", str(res), "--dataset", str(DATASET_DIR)]

    assert cli.main([*base, "--dry-run"]) == 0
    assert "Jev 대상=2" in capsys.readouterr().out
    assert not (tmp_path / "r.graded.jsonl").exists() and not (tmp_path / "r.grade").exists()

    hooks = {
        "transport": httpx.MockTransport(jev_handler),
        "llm_factory": lambda _m: FakeGemini(),
    }
    args = cli._parser().parse_args([*base, "--max-cost-usd", "1"])
    assert cli._grade(args, **hooks) == 0
    out = [json.loads(x) for x in (tmp_path / "r.graded.jsonl").read_text().splitlines()]
    assert [o["grade"]["rule"] for o in out] == ["gemini", "jev", "code"]
    assert [o["outcome"]["success"] for o in out] == [True, True, False]
    assert out[2]["outcome"]["failure_type"] == "wrong_state_change"
    assert "합성 답" not in json.dumps([o["grade"] for o in out])  # grade에 답 원문 없음
    assert FakeGemini.n == 1
    assert res.read_text() == "".join(json.dumps(r) + "\n" for r in ARCH_FACT_ROWS)  # 원본 그대로

    assert cli._grade(args, **hooks) == 2  # 덮어쓰지 않는다
    (tmp_path / "r.graded.jsonl").unlink()
    assert cli._grade(args, **hooks) == 0  # 저장된 판정을 재사용한다
    assert FakeGemini.n == 1


def test_상한이_모자라면_부분_파일_없이_코드_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(jev.KEY_ENV, "test-key")
    monkeypatch.setenv("ORBIT_OPENROUTER_API_KEY", "test-key")
    res = tmp_path / "r.jsonl"
    res.write_text("".join(json.dumps(r) + "\n" for r in ARCH_FACT_ROWS), encoding="utf-8")
    args = ["grade", str(res), "--dataset", str(DATASET_DIR), "--max-cost-usd", "1e-12"]
    assert cli.main(args) == 3
    assert not (tmp_path / "r.graded.jsonl").exists()


def reset(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv(jev.KEY_ENV, "test-key")
    monkeypatch.setenv("ORBIT_OPENROUTER_API_KEY", "test-key")
    JEV_CALLS.clear()
    FAIL_FIRST[0] = False
    FakeGemini.n = 0
    return {"transport": httpx.MockTransport(jev_handler), "llm_factory": lambda _m: FakeGemini()}


def write_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def grade_args(res: Path, *extra: str) -> argparse.Namespace:
    return cli._parser().parse_args(["grade", str(res), "--dataset", str(DATASET_DIR), *extra])


def test_Gemini_입력만_설명_겹침과_관계없이_제목을_붙인다() -> None:
    raw = [
        {"fact_id": "a", "description": "일정 제목", "any_of": ["회의"]},
        {"fact_id": "b", "description": "일정 시작 시각", "any_of": ["10시"]},
    ]
    gt = fg.GradeTask(
        "t1", "q", fg.context_facts(raw), fg.context_facts(raw, always=True), None, None
    )
    (e,) = fg.plan([row()], {"t1": gt}, "res")
    assert e.task and e.gemini_task
    assert e.task.facts[1].description == "일정 시작 시각"  # Jev: 겹치지 않으면 그대로
    assert e.gemini_task.facts[1].description == "'회의' 일정의 시작 시각"


def test_지문에_규칙_버전이_들어간다(monkeypatch: pytest.MonkeyPatch) -> None:
    model = judge_models(str(fg.GEMINI_CONFIG), ["google"])["google"]
    before = fg.fingerprint(model)
    monkeypatch.setattr(fg, "RULE_VERSION", "agent-final-v2")
    assert fg.fingerprint(model) != before


def test_호출_실패로_빠진_판정이_있으면_파일_없이_코드_3_재실행은_빠진_건만(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = reset(monkeypatch)
    res = tmp_path / "r.jsonl"
    write_rows(res, ARCH_FACT_ROWS)
    args = grade_args(res, "--max-cost-usd", "1")
    FAIL_FIRST[0] = True
    assert cli._grade(args, **hooks) == 3
    assert not (tmp_path / "r.graded.jsonl").exists()
    assert len(JEV_CALLS) == 2
    JEV_CALLS.clear()
    FAIL_FIRST[0] = False
    assert cli._grade(args, **hooks) == 0
    assert len(JEV_CALLS) == 1  # 실패한 1건만 다시 부른다
    assert (tmp_path / "r.graded.jsonl").exists()


def test_빈_답의_null은_정상이라_파일을_쓴다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = reset(monkeypatch)
    res = tmp_path / "r.jsonl"
    write_rows(res, [row(task_id="t-002", text="")])
    assert cli._grade(grade_args(res, "--max-cost-usd", "1"), **hooks) == 0
    (out,) = [json.loads(x) for x in (tmp_path / "r.graded.jsonl").read_text().splitlines()]
    assert out["outcome"]["success"] is None and JEV_CALLS == []


def test_test_분할_행은_봉인_플래그가_없으면_호출_전에_거부(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = reset(monkeypatch)
    res = tmp_path / "r.jsonl"
    write_rows(res, [row(task_id="t-004", split="test")])
    with pytest.raises(SealedSplitError):
        cli._grade(grade_args(res, "--max-cost-usd", "1"), **hooks)
    assert JEV_CALLS == [] and not (tmp_path / "r.graded.jsonl").exists()
    assert cli.main(["grade", str(res), "--dataset", str(DATASET_DIR), "--max-cost-usd", "1"]) == 2
    assert not (tmp_path / "r.grade").exists()
    flagged = grade_args(res, "--max-cost-usd", "1", cli.SEAL_FLAG)
    assert cli._grade(flagged, **hooks) == 0
    assert (tmp_path / "r.graded.jsonl").exists()


def test_이전_Jev_비용이_상한_계산에_반영된다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = reset(monkeypatch)
    res = tmp_path / "r.jsonl"
    write_rows(res, ARCH_FACT_ROWS)
    (tmp_path / "r.grade").mkdir()
    prev = {"item_id": "x/dev/other/0", "repeat_index": 0, "prompt_version": jev.V4,
            "status": "error", "cost": 1.0}  # fmt: skip
    (tmp_path / "r.grade" / jev.JEV_FILE).write_text(json.dumps(prev) + "\n", encoding="utf-8")
    assert cli._grade(grade_args(res, "--max-cost-usd", "1"), **hooks) == 3
    assert JEV_CALLS == []
    assert cli._grade(grade_args(res, "--max-cost-usd", "2"), **hooks) == 0


def test_Gemini_단계에서_남은_상한이_부족하면_코드_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = reset(monkeypatch)
    res = tmp_path / "r.jsonl"
    write_rows(res, ARCH_FACT_ROWS)
    assert cli._grade(grade_args(res, "--max-cost-usd", "0.0001"), **hooks) == 3
    assert len(JEV_CALLS) == 2 and FakeGemini.n == 0  # Jev는 끝났고 Gemini는 부르지 않았다
    assert not (tmp_path / "r.graded.jsonl").exists()


def live_run_args(tmp_path: Path, *extra: str) -> argparse.Namespace:
    script = tmp_path / "s.json"
    script.write_text(json.dumps(good_script(), ensure_ascii=False), encoding="utf-8")
    return cli._parser().parse_args(
        [
            "run",
            "--dataset",
            str(DATASET_DIR),
            "--split",
            "dev",
            "--sut",
            "scripted_tool",
            "--repeats",
            "1",
            "--out",
            str(tmp_path / "o.jsonl"),
            "--llm-script",
            str(script),
            "--grade",
            "llm",
            *extra,
        ]  # fmt: skip
    )


def test_run_grade_llm이_실행_뒤_채점까지_이어진다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = reset(monkeypatch)
    assert cli._run(live_run_args(tmp_path, "--max-grade-cost-usd", "1"), **hooks) == 0
    run_rows = (tmp_path / "o.jsonl").read_text().splitlines()
    graded = [json.loads(x) for x in (tmp_path / "o.graded.jsonl").read_text().splitlines()]
    assert len(graded) == len(run_rows) > 0
    assert all("grade" in g for g in graded) and len(JEV_CALLS) > 0


def test_run_끝_채점이_상한으로_실패하면_다시_채점하는_방법을_알린다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hooks = reset(monkeypatch)
    assert cli._run(live_run_args(tmp_path, "--max-grade-cost-usd", "1e-12"), **hooks) == 3
    assert (tmp_path / "o.jsonl").exists() and not (tmp_path / "o.graded.jsonl").exists()
    err = capsys.readouterr().err
    assert "orbit.evalrunner grade" in err and str(tmp_path / "o.jsonl") in err


def test_graded_임시_파일은_실패해도_남지_않는다(tmp_path: Path) -> None:
    path = tmp_path / "r.graded.jsonl"
    path.write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        fg.write_graded(path, [{"a": 1}])
    assert [p.name for p in tmp_path.iterdir()] == ["r.graded.jsonl"]
    fg.write_graded(tmp_path / "new.graded.jsonl", [{"a": 1}])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["new.graded.jsonl", "r.graded.jsonl"]


def test_입력이_바뀐_옛_Jev_판정은_호출_전에_거부한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = reset(monkeypatch)
    res = tmp_path / "r.jsonl"
    write_rows(res, ARCH_FACT_ROWS)
    args = grade_args(res, "--max-cost-usd", "1")
    assert cli._grade(args, **hooks) == 0
    jfile = tmp_path / "r.grade" / jev.JEV_FILE
    rows = [json.loads(x) for x in jfile.read_text().splitlines()]
    rows[0]["input_sha"] = "stale"
    write_rows(jfile, rows)
    (tmp_path / "r.graded.jsonl").unlink()
    JEV_CALLS.clear()
    assert cli._grade(args, **hooks) == 2
    assert JEV_CALLS == [] and not (tmp_path / "r.graded.jsonl").exists()


def test_run_끝_채점이_GradeError_밖의_예외여도_재채점_방법을_알리고_다시_던진다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hooks = reset(monkeypatch)

    def boom(*_a: Any, **_k: Any) -> Path:
        raise OSError("disk")

    monkeypatch.setattr(fg, "write_graded", boom)
    with pytest.raises(OSError):
        cli._run(live_run_args(tmp_path, "--max-grade-cost-usd", "1"), **hooks)
    err = capsys.readouterr().err
    assert "orbit.evalrunner grade" in err and str(tmp_path / "o.jsonl") in err


def test_Gemini_이력_비용도_Jev_단계_상한에서_뺀다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = reset(monkeypatch)
    res = tmp_path / "r.jsonl"
    write_rows(res, ARCH_FACT_ROWS)
    (tmp_path / "r.grade").mkdir()
    prev = {"item_id": "x/dev/other/0", "reviewer_family": "google", "cost": 1.0}
    write_rows(tmp_path / "r.grade" / JUDGED_FILE, [prev])
    assert cli._grade(grade_args(res, "--max-cost-usd", "1"), **hooks) == 3
    assert JEV_CALLS == []


def test_데이터셋_분할이_test인_과제도_봉인한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = reset(monkeypatch)
    res = tmp_path / "r.jsonl"
    write_rows(res, [row(task_id="t-004", split="dev")])  # 행 split은 dev로 위장
    with pytest.raises(SealedSplitError):
        cli._grade(grade_args(res, "--max-cost-usd", "1"), **hooks)
    assert JEV_CALLS == []


def test_repeat가_없는_행은_GradeError() -> None:
    r = row()
    del r["repeat"]
    with pytest.raises(fg.GradeError):
        fg.plan([r], {"t1": TASK}, "res")


def test_graded_파일은_일반_결과_파일_권한으로_쓴다(tmp_path: Path) -> None:
    import os

    umask = os.umask(0o022)
    try:
        p = fg.write_graded(tmp_path / "a.graded.jsonl", [{"a": 1}])
    finally:
        os.umask(umask)
    assert p.stat().st_mode & 0o777 == 0o644
