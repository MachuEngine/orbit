"""평가 v1 자동 검수·표본·결정 반영·봉인 테스트 (ADR-0026 §8). 실제 LLM은 부르지 않는다."""

import asyncio
import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from orbit.evaldata.dataset import DatasetError, load_dataset, read_review
from orbit.evaldata.v1 import auto_review as ar
from orbit.evaldata.v1 import review_packet as rp
from orbit.evaldata.v1.realize_models import FamilyModel, ModelConfigError, default_models
from orbit.evaldata.v1.search_set import load_search_set, read_search_review
from orbit.llm.interface import MalformedResponseError, ModelMismatchError, TransientLLMError
from orbit.llm.types import LLMRequest, LLMResponse, LLMUsage

REPO = Path(__file__).resolve().parents[5]
COMMITTED = REPO / "eval" / "datasets" / "orbit-eval" / "v1"
BIG_FILES = ("world.json", "skeleton.json")
SMALL_FILES = (
    "manifest.json",
    "tasks.jsonl",
    "search.jsonl",
    "search-manifest.json",
    "paraphrase.jsonl",
)
MODELS = default_models()
USAGE = LLMUsage(input_tokens=800, output_tokens=60)


# ---------------------------------------------------------------- 픽스처


@pytest.fixture(scope="module")
def corpus() -> ar.Corpus:
    return ar.load_corpus(COMMITTED)


@pytest.fixture(scope="module")
def items(corpus: ar.Corpus) -> list[ar.ReviewItem]:
    return corpus.items()


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """큰 파일은 심볼릭 링크, 작은 파일은 복사한 데이터셋 폴더. review·seal 기록 없이 시작한다."""
    out = tmp_path / "ds"
    out.mkdir()
    for name in BIG_FILES:
        (out / name).symlink_to(COMMITTED / name)
    for name in SMALL_FILES:
        shutil.copy(COMMITTED / name, out / name)
    # 커밋된 데이터셋에 이미 검수·봉인 기록이 있어도 처음 상태에서 시작한다.
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest.pop("seal", None)
    (manifest.get("details") or {}).pop("review", None)
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    sm = json.loads((out / "search-manifest.json").read_text(encoding="utf-8"))
    sm.pop("seal", None)
    sm.pop("review", None)
    (out / "search-manifest.json").write_text(json.dumps(sm, ensure_ascii=False), encoding="utf-8")
    return out


def pick(items: list[ar.ReviewItem], kind: str, item_type: str | None = None) -> ar.ReviewItem:
    return next(
        i for i in items if i.kind == kind and (item_type is None or i.item_type == item_type)
    )


def task_named(corpus: ar.Corpus, task_id: str) -> Any:
    return next(t for t in corpus.tasks if t.task_id == task_id)


# ---------------------------------------------------------------- 꾸러미


def test_calendar_packet_shows_local_time_and_all_turns(corpus: ar.Corpus) -> None:
    text = rp.task_packet(corpus.tw, task_named(corpus, "mt-day-agenda-0001"))
    assert "턴1: 11월 16일 잡힌 일정 전부 알려줘" in text
    assert "턴2: 그 일정들 중에서 " in text and "시작하는 것만 다시 알려줘" in text
    assert '일정 "분기 계획 점검" 2026-11-16(월) 14:30~16:00' in text  # UTC가 아니라 현지 시각
    assert "사용자: 윤서진 (시간대 Asia/Seoul)" in text
    assert "기준 시각(as_of): 2026-11-13(금) 11:30" in text


def test_timezone_task_uses_user_zone(corpus: ar.Corpus) -> None:
    text = rp.task_packet(corpus.tw, task_named(corpus, "t-tz-event-0003"))
    assert "America/Los_Angeles" in text
    assert "2026-10-20(화) 08:30" in text


def test_mail_packet_has_subject_sender_date_and_excerpt(corpus: ar.Corpus) -> None:
    text = rp.task_packet(corpus.tw, task_named(corpus, "t-mail-fact-0003"))
    assert '메일 "Re: [이정표] 배포 계획서 안내" 발신 박지수 2026-10-19(월) 21:15' in text
    assert "회의실 3층 B" in text
    assert "응답에 있으면 안 되는 표현: 회의실 3층 A" in text


def test_ambiguous_packet_lists_both_readings(corpus: ar.Corpus) -> None:
    text = rp.task_packet(corpus.tw, task_named(corpus, "t-ambiguous-0003"))
    assert "해석 1 (event)" in text and "해석 2 (event_set)" in text
    assert '"설계 리뷰 - 고지원"' in text and '"기술 논의 - 고지원"' in text


def _gold_keys(node: Any) -> set[str]:
    """정답·기대 변화 JSON에서 참조하는 일정·메일·문서 ID를 모은다."""
    found: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str) and v:
                prefix = {
                    "event_id": "ref_event",
                    "message_id": "ref_message",
                    "reply_to_message_id": "ref_message",
                    "file_id": "ref_file",
                }.get(k)
                if prefix:
                    found.add(f"{prefix}:{v}")
            found |= _gold_keys(v)
    elif isinstance(node, list):
        for v in node:
            found |= _gold_keys(v)
    return found


def test_every_gold_artifact_is_in_the_evidence_section(corpus: ar.Corpus) -> None:
    originals = [t for t in corpus.tasks if rp.task_origin(t) is None]
    assert len(originals) > 100
    for task in originals:
        needed = _gold_keys(task.gold.model_dump(mode="json"))
        got = set(rp.task_evidence_keys(corpus.tw, task))
        assert needed <= got, f"{task.task_id}: 근거에 빠진 정답 아티팩트 {sorted(needed - got)}"
        assert not needed or "(정답 근거)" in rp.task_packet(corpus.tw, task)


def test_mismatch_packet_has_calendar_event_and_mail(corpus: ar.Corpus) -> None:
    text = rp.task_packet(corpus.tw, task_named(corpus, "t-mismatch-0006"))
    evidence = text.split("근거 자료:")[1]
    assert '(정답 근거) [캘린더 일정] 일정 "데모 리허설"' in evidence
    assert '일정 "데모 리허설" 2026-11-23(월) 10:30~11:30' in evidence
    assert "캘린더 시간대 America/Los_Angeles" in evidence
    assert '메일 "데모 리허설 일정 안내"' in evidence


def test_mail_fact_packet_has_latest_and_previous(corpus: ar.Corpus) -> None:
    packets = [
        rp.task_packet(corpus.tw, t)
        for t in corpus.origins()
        if "scenario:mail_fact" in t.tags and t.task_id.startswith("t-")
    ]
    both = [x.split("근거 자료:")[1] for x in packets if "[이전 값 메일]" in x]
    assert both, "이전 값 메일이 있는 mail_fact 과제가 하나는 있어야 한다"
    for evidence in both:
        latest, previous = evidence.split("[이전 값 메일]", 1)
        assert "[최신 값 메일]" in latest
        assert previous.strip() and previous != latest


def _origins_with(corpus: ar.Corpus, tag: str, *, writes: bool | None = None) -> list[Any]:
    out = [t for t in corpus.origins() if tag in t.tags]
    if writes is not None:
        out = [t for t in out if bool(t.gold.expected_changes) == writes]
    assert out, tag
    return out


def test_reply_packet_marks_the_target_mail_and_what_is_graded(corpus: ar.Corpus) -> None:
    for task in _origins_with(corpus, "scenario:reply_needed", writes=True):
        text = rp.task_packet(corpus.tw, task)
        change = task.gold.expected_changes[0]
        assert "답장 대상 메일(사용자가 받은 메일" in text
        assert "수신자·답장 대상만 채점, 답장 본문 사실: 없음" in text
        # 수신자 확인에 쓰는 발신자 주소가 대상 메일 줄에 있다
        target = next(x for x in text.splitlines() if "답장 대상 메일(" in x)
        assert f"<{change.to_include[0]}>" in target
    for task in _origins_with(corpus, "scenario:mail_fact", writes=True):
        text = rp.task_packet(corpus.tw, task)
        fact = task.gold.expected_changes[0].body_facts[0]
        assert f"답장 본문 사실: {fact.description}={fact.any_of[0]}" in text
        assert "답장 본문 사실: 없음" not in text


def test_mismatch_packet_states_the_calendar_wins_rule_only_for_mismatch(
    corpus: ar.Corpus,
) -> None:
    for task in _origins_with(corpus, "scenario:mismatch"):
        text = rp.task_packet(corpus.tw, task)
        assert "메일과 캘린더가 어긋나면 캘린더에 잡힌 값이 정답" in text
        assert text.index("규칙: 메일과 캘린더") < text.index("정답:")
    other = rp.task_packet(corpus.tw, _origins_with(corpus, "scenario:day_agenda")[0])
    assert "캘린더에 잡힌 값이 정답" not in other


def test_event_update_packet_shows_start_end_and_the_length_rule(corpus: ar.Corpus) -> None:
    moves = [
        t
        for t in _origins_with(corpus, "scenario:event_change_target", writes=True)
        if t.gold.expected_changes[0].kind == "event_updated"
    ]
    assert moves
    for task in moves:
        text = rp.task_packet(corpus.tw, task)
        line = next(x for x in text.splitlines() if x.startswith("- 일정 변경:"))
        assert "시작 " in line and "종료 " in line
        assert "원래 길이" in line and "원래 일정 길이를 유지" in line


def test_changed_packet_is_reviewed_again_and_unchanged_ones_keep_their_verdict(
    items: list[ar.ReviewItem],
) -> None:
    import dataclasses

    first = pick(items, "task", "mail")
    second = next(i for i in items if i.kind == "task" and i.item_id != first.item_id)
    rows = [row_for(i, fam) for i in (first, second) for fam in i.reviewers]
    states = ar.item_states([first, second], rows)
    assert states[first.item_id].status == states[second.item_id].status == "pass"
    edited = dataclasses.replace(first, packet=first.packet + "\n규칙: 새로 추가한 줄")
    states = ar.item_states([edited, second], rows)
    assert states[edited.item_id].status == "incomplete"
    assert states[second.item_id].status == "pass"


def test_abstain_packet_names_the_missing_target(corpus: ar.Corpus) -> None:
    text = rp.task_packet(corpus.tw, task_named(corpus, "t-absent-0002"))
    assert "기권이 정답이다" in text and "장민규님과의 미팅" in text


def test_write_packet_shows_expected_changes(corpus: ar.Corpus) -> None:
    text = rp.task_packet(corpus.tw, task_named(corpus, "t-meeting-request-0005"))
    assert "쓰기 기대 변화:" in text
    assert (
        '일정 생성: "기술 논의 미팅" 2026-12-02(수) 11:00 시작(종료 시각은 채점하지 않음)' in text
    )
    reminder = rp.task_packet(corpus.tw, task_named(corpus, "t-deadline-0005"))
    assert "리마인더 생성: 2026-11-15(일) 09:00" in reminder


def test_excerpt_is_capped_and_follows_the_fact() -> None:
    body = "앞부분 " * 400 + "핵심 값 8,500,000원 " + "뒷부분 " * 400
    short = rp.excerpt(body, 1200)
    assert len(short) <= 1202 and "8,500,000원" not in short
    focused = rp.excerpt(body, 1200, ["8,500,000원"])
    assert len(focused) <= 1202 and "8,500,000원" in focused and focused.startswith("…")
    assert rp.excerpt("짧은 글", 1200) == "짧은 글"


def test_packets_respect_excerpt_limit(corpus: ar.Corpus, items: list[ar.ReviewItem]) -> None:
    long_task = task_named(corpus, "t-deadline-0001")
    llm = rp.task_packet(corpus.tw, long_task, excerpt_chars=rp.LLM_EXCERPT_CHARS)
    human = rp.task_packet(corpus.tw, long_task, excerpt_chars=rp.HUMAN_EXCERPT_CHARS)
    assert len(human) < len(llm)
    for item in items:
        for line in item.packet.splitlines():
            if line.strip().startswith("본문:"):
                assert len(line) <= rp.LLM_EXCERPT_CHARS + 20


def test_search_packet_has_both_grades_and_form(items: list[ar.ReviewItem]) -> None:
    it = next(i for i in items if i.item_id == "sc-search-topic-0001-keyword")
    assert "형태 키워드형" in it.packet
    assert "질의: 달빛정원 시연 일정 최신 최종 금액 자료" in it.packet
    assert "8,500,000원" in it.packet  # 등급 2 문서 발췌
    assert "등급 1 문서" in it.packet and "8,100,000원" in it.packet


def test_items_cover_originals_and_queries_only(
    corpus: ar.Corpus, items: list[ar.ReviewItem]
) -> None:
    assert sum(i.kind == "task" for i in items) == 192
    assert sum(i.kind == "search" for i in items) == 180
    task_ids = {i.item_id for i in items if i.kind == "task"}
    assert not any(i.endswith(("-fault", "-para")) for i in task_ids)


# ---------------------------------------------------------------- 검수 계열·판정


def test_reviewers_exclude_generator_family(items: list[ar.ReviewItem]) -> None:
    for it in items:
        assert it.family in ("openai", "anthropic", "google")
        assert len(it.reviewers) == 2
        assert it.family not in it.reviewers


def test_verdict_fields_by_kind(items: list[ar.ReviewItem]) -> None:
    assert pick(items, "task", "mail").fields == ar.TASK_FIELDS
    assert pick(items, "task", "ambiguous").fields == ar.AMBIGUOUS_FIELDS
    assert pick(items, "search").fields == ar.SEARCH_FIELDS


def ok_json(item: ar.ReviewItem, **override: str) -> str:
    data: dict[str, str] = dict.fromkeys(item.fields, "ok")
    data.update(override)
    data["reason"] = ""
    return json.dumps(data, ensure_ascii=False)


def test_parse_accepts_plain_fenced_and_wrapped_json(items: list[ar.ReviewItem]) -> None:
    it = pick(items, "task", "mail")
    body = ok_json(it, gold_valid="unsure")
    for text in (body, f"```json\n{body}\n```", f"판정입니다.\n{body}\n끝"):
        parsed = ar.parse_verdict(it, text)
        assert parsed is not None and parsed[0]["gold_valid"] == "unsure"


def test_parse_rejects_bad_values_and_missing_fields(items: list[ar.ReviewItem]) -> None:
    it = pick(items, "task", "mail")
    assert ar.parse_verdict(it, None) is None
    assert ar.parse_verdict(it, "ok") is None
    assert ar.parse_verdict(it, "{not json}") is None
    assert ar.parse_verdict(it, ok_json(it, natural="unsure")) is None  # unsure는 gold_valid만
    assert ar.parse_verdict(it, ok_json(it, natural="maybe")) is None
    missing = json.loads(ok_json(it))
    missing.pop("single_kind")
    assert ar.parse_verdict(it, json.dumps(missing)) is None
    amb = pick(items, "task", "ambiguous")
    assert ar.parse_verdict(amb, ok_json(it)) is None  # single_kind만 있고 readings_natural이 없다


def test_parse_truncates_reason_to_120_chars(items: list[ar.ReviewItem]) -> None:
    it = pick(items, "search")
    data = json.loads(ok_json(it))
    data["reason"] = "가" * 300
    parsed = ar.parse_verdict(it, json.dumps(data, ensure_ascii=False))
    assert parsed is not None and len(parsed[1]) == 120


# ---------------------------------------------------------------- 실행


def reply(text: str | None, *, cost: float | None = None, finish: str = "stop") -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=(),
        finish_reason=finish,  # type: ignore[arg-type]
        usage=USAGE,
        cost_usd=cost,
        latency_ms=1,
        model_id="m",
        provider=None,
    )


Handler = Callable[[str, str, int, LLMRequest], LLMResponse | Exception]


class FakeLLM:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[LLMRequest] = []
        self.calls: dict[tuple[str, str], int] = {}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        key = (request.metadata["item_id"], request.metadata["reviewer"])
        n = self.calls.get(key, 0)
        self.calls[key] = n + 1
        out = self.handler(key[0], key[1], n, request)
        if isinstance(out, Exception):
            raise out
        return out


def answer_for(by_id: dict[str, ar.ReviewItem]) -> Handler:
    def handler(item_id: str, _fam: str, _n: int, _req: LLMRequest) -> LLMResponse:
        return reply(ok_json(by_id[item_id]))

    return handler


def factory_for(llm: FakeLLM) -> Callable[[FamilyModel], FakeLLM]:
    return lambda model: llm


async def no_sleep(_: float) -> None:
    return None


def cfg(cap: float = 10.0, concurrency: int = 1) -> ar.ReviewConfig:
    return ar.ReviewConfig(max_cost_usd=cap, concurrency=concurrency, transient_backoff_seconds=0)


def run(
    items: list[ar.ReviewItem], out: Path, llm: FakeLLM, config: ar.ReviewConfig
) -> ar.RunSummary:
    return asyncio.run(
        ar.run_reviews(items, out, MODELS, config, llm_factory=factory_for(llm), sleep=no_sleep)
    )


@pytest.fixture
def six(items: list[ar.ReviewItem]) -> list[ar.ReviewItem]:
    return ar.select_items(items, 6, 1)


def test_run_uses_the_two_other_families_and_records_rows(
    six: list[ar.ReviewItem], tmp_path: Path
) -> None:
    by_id = {i.item_id: i for i in six}
    llm = FakeLLM(answer_for(by_id))
    summary = run(six, tmp_path, llm, cfg())
    assert summary.stop_reason is None and summary.processed == 12
    for req in llm.requests:
        item = by_id[req.metadata["item_id"]]
        assert req.metadata["reviewer"] != item.family  # 자기 계열은 검수하지 않는다
        assert req.metadata["prompt_version"] == "review-v1"
        assert item.packet in req.messages[1].content  # type: ignore[operator]
    rows = ar.read_rows(tmp_path / ar.AUTO_FILE)
    assert len(rows) == 12
    row = rows[0]
    assert {"item_id", "item_kind", "reviewer_family", "model_id", "verdict", "reason"} <= set(row)
    assert {"tokens", "cost"} <= set(row)
    assert "text" not in row and "response" not in row  # 응답 원문은 저장하지 않는다
    assert all(s.status == "pass" for s in ar.item_states(six, rows).values())


def test_parse_retry_then_success(six: list[ar.ReviewItem], tmp_path: Path) -> None:
    by_id = {i.item_id: i for i in six}
    target = six[0].item_id

    def handler(item_id: str, fam: str, n: int, req: LLMRequest) -> LLMResponse:
        if item_id == target and n < 2:
            return reply("판정 불가")
        return reply(ok_json(by_id[item_id]))

    llm = FakeLLM(handler)
    run(six[:1], tmp_path, llm, cfg())
    rows = ar.read_rows(tmp_path / ar.AUTO_FILE)
    assert [r["attempts"] for r in rows] == [3, 3]
    assert all(not r["parse_failed"] and r["status"] == "ok" for r in rows)
    assert len(llm.requests) == 6  # 두 계열 × 3회


def test_parse_failure_after_retries_is_unsure_and_flagged(
    six: list[ar.ReviewItem], tmp_path: Path
) -> None:
    llm = FakeLLM(lambda *_: reply("json이 아님"))
    item = six[0]
    summary = run([item], tmp_path, llm, cfg())
    rows = ar.read_rows(tmp_path / ar.AUTO_FILE)
    assert summary.parse_failed == 2 and len(llm.requests) == 6  # 최대 2회 재시도
    for r in rows:
        assert r["parse_failed"] is True and r["status"] == "ok"
        assert set(r["verdict"].values()) == {"unsure"}
    assert ar.item_states([item], rows)[item.item_id].status == "flagged"


def test_truncated_response_counts_as_parse_failure(
    six: list[ar.ReviewItem], tmp_path: Path
) -> None:
    by_id = {i.item_id: i for i in six}
    llm = FakeLLM(lambda i, f, n, r: reply(ok_json(by_id[i]), finish="length"))
    run(six[:1], tmp_path, llm, cfg())
    assert all(r["parse_failed"] for r in ar.read_rows(tmp_path / ar.AUTO_FILE))


def row_for(
    item: ar.ReviewItem, family: str, verdict: dict[str, str] | None = None
) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "item_kind": item.kind,
        "reviewer_family": family,
        "status": "ok",
        "verdict": verdict or dict.fromkeys(item.fields, "ok"),
        "reason": "",
        "prompt_version": ar.PROMPT_VERSION,
        "packet_sha": item.packet_sha(),
        "tokens": {"input": 1, "output": 1},
        "cost": 0.0,
    }


def test_flag_rule(items: list[ar.ReviewItem]) -> None:
    it = pick(items, "task", "mail")
    a, b = it.reviewers
    clean = [row_for(it, a), row_for(it, b)]
    assert ar.item_states([it], clean)[it.item_id].status == "pass"
    for field, value in (("natural", "issue"), ("gold_valid", "unsure"), ("single_kind", "issue")):
        bad = dict.fromkeys(it.fields, "ok") | {field: value}
        rows = [row_for(it, a), row_for(it, b, bad)]
        assert ar.item_states([it], rows)[it.item_id].status == "flagged"
    assert ar.item_states([it], clean[:1])[it.item_id].status == "incomplete"
    stale = {**clean[1], "packet_sha": "other"}
    assert ar.item_states([it], [clean[0], stale])[it.item_id].status == "incomplete"
    aborted = {**clean[1], "status": "aborted", "verdict": {}}
    assert ar.item_states([it], [clean[0], aborted])[it.item_id].status == "incomplete"


def test_cost_cap_stops_and_resume_skips_finished_work(
    six: list[ar.ReviewItem], tmp_path: Path
) -> None:
    by_id = {i.item_id: i for i in six}
    llm = FakeLLM(answer_for(by_id))
    one_call = MODELS["openai"].cost_usd(USAGE)
    first = run(six, tmp_path, llm, cfg(cap=one_call * 0.5))
    assert first.stop_reason == "cost_cap" and first.processed == 1
    calls_before = len(llm.requests)
    assert calls_before == 1
    second = run(six, tmp_path, llm, cfg(cap=10.0))
    assert second.stop_reason is None
    rows = ar.read_rows(tmp_path / ar.AUTO_FILE)
    done = {(r["item_id"], r["reviewer_family"]) for r in rows if r["status"] == "ok"}
    assert len(done) == 12
    assert len(llm.requests) == 12  # 이미 끝난 첫 작업은 다시 부르지 않았다
    third = run(six, tmp_path, llm, cfg(cap=10.0))
    assert third.processed == 0 and len(llm.requests) == 12


def test_cap_includes_prior_spend_and_aborted_rows_are_redone(
    six: list[ar.ReviewItem], tmp_path: Path
) -> None:
    by_id = {i.item_id: i for i in six}
    llm = FakeLLM(answer_for(by_id))
    run(six, tmp_path, llm, cfg(cap=0.0000001, concurrency=2))
    rows = ar.read_rows(tmp_path / ar.AUTO_FILE)
    assert any(r["status"] == "aborted" for r in rows)
    spent = ar.total_cost(rows)
    # 누적 지출이 이미 상한 이상이면 아무것도 부르지 않는다
    before = len(llm.requests)
    stopped = run(six, tmp_path, llm, cfg(cap=spent))
    assert stopped.stop_reason == "cost_cap" and len(llm.requests) == before
    run(six, tmp_path, llm, cfg(cap=10.0))
    final = ar.latest_rows(ar.read_rows(tmp_path / ar.AUTO_FILE))
    assert len(final) == 12 and all(r["status"] == "ok" for r in final.values())


def test_reported_cost_wins_when_larger(six: list[ar.ReviewItem], tmp_path: Path) -> None:
    by_id = {i.item_id: i for i in six}
    llm = FakeLLM(lambda i, f, n, r: reply(ok_json(by_id[i]), cost=0.5))
    run(six[:1], tmp_path, llm, cfg())
    assert all(r["cost"] == 0.5 for r in ar.read_rows(tmp_path / ar.AUTO_FILE))


def test_transient_error_retries_then_charges(six: list[ar.ReviewItem], tmp_path: Path) -> None:
    by_id = {i.item_id: i for i in six}

    def handler(item_id: str, fam: str, n: int, req: LLMRequest) -> LLMResponse | Exception:
        if n == 0:
            return TransientLLMError("일시 오류", usage=USAGE)
        return reply(ok_json(by_id[item_id]))

    llm = FakeLLM(handler)
    summary = run(six[:1], tmp_path, llm, cfg())
    assert summary.stop_reason is None
    rows = ar.read_rows(tmp_path / ar.AUTO_FILE)
    assert len(rows) == 2 and all(r["status"] == "ok" for r in rows)
    assert all(r["cost"] > MODELS[r["reviewer_family"]].cost_usd(USAGE) for r in rows)


def test_model_config_change_is_refused(six: list[ar.ReviewItem], tmp_path: Path) -> None:
    by_id = {i.item_id: i for i in six}
    run(six[:1], tmp_path, FakeLLM(answer_for(by_id)), cfg())
    other = {
        **MODELS,
        "openai": default_models()["openai"].__class__(
            **{**MODELS["openai"].__dict__, "price_output_per_mtok": 9.0}
        ),
    }
    with pytest.raises(ModelConfigError):
        asyncio.run(ar.run_reviews(six[:1], tmp_path, other, cfg()))


def test_select_items_is_stratified_and_deterministic(items: list[ar.ReviewItem]) -> None:
    a = ar.select_items(items, 30, 7)
    assert a == ar.select_items(items, 30, 7)
    assert a != ar.select_items(items, 30, 8)
    assert len(a) == 30
    strata = {(i.kind, i.item_type) for i in a}
    assert len(strata) >= 12  # 종단 유형 9개 + 검색 형태 3개를 골고루
    assert ar.select_items(items, 10_000, 7) == items


def test_estimate_skips_finished_work(items: list[ar.ReviewItem]) -> None:
    full = ar.estimate_cost(items, MODELS)
    assert set(full["families"]) == {"openai", "anthropic", "google"}
    assert sum(f["calls"] for f in full["families"].values()) == 2 * len(items)
    it = items[0]
    rows = [row_for(it, f) for f in it.reviewers]
    less = ar.estimate_cost(items, MODELS, rows)
    assert sum(f["calls"] for f in less["families"].values()) == 2 * len(items) - 2
    assert less["total_cost_usd"] < full["total_cost_usd"]


def test_dry_run_prints_estimate_and_writes_nothing(
    work: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = ar.main(["--dataset-dir", str(work), "--max-cost-usd", "1", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0 and "--dry-run" in out
    assert all(f in out for f in ("openai", "anthropic", "google")) and "예상 총 $" in out
    assert not (work / ar.AUTO_FILE).exists() and not (work / ar.AUTO_MANIFEST).exists()


def test_cli_requires_a_cost_cap(work: Path) -> None:
    with pytest.raises(SystemExit):
        ar.main(["--dataset-dir", str(work), "--dry-run"])
    assert (
        ar.main(["--dataset-dir", str(work), "--max-cost-usd", "0", "--dry-run"]) == ar.EXIT_USAGE
    )


def test_manifest_summarizes_models_prices_and_counts(
    six: list[ar.ReviewItem], tmp_path: Path
) -> None:
    by_id = {i.item_id: i for i in six}
    bad = six[0].item_id

    def handler(item_id: str, fam: str, n: int, req: LLMRequest) -> LLMResponse:
        v = (
            ok_json(by_id[item_id], **{by_id[item_id].fields[0]: "issue"})
            if item_id == bad
            else ok_json(by_id[item_id])
        )
        return reply(v)

    run(six, tmp_path, FakeLLM(handler), cfg())
    manifest = ar.write_auto_manifest(tmp_path, six, MODELS, ar.read_rows(tmp_path / ar.AUTO_FILE))
    saved = json.loads((tmp_path / ar.AUTO_MANIFEST).read_text(encoding="utf-8"))
    assert saved == manifest
    assert manifest["prompt_version"] == "review-v1"
    assert manifest["flagged"] == 1 and manifest["passed"] == 5 and manifest["incomplete"] == 0
    assert set(manifest["families"]) == {"openai", "anthropic", "google"}
    assert manifest["families"]["openai"]["model_config"]["price_input_per_mtok"] > 0
    assert manifest["spent_usd"] > 0


# ---------------------------------------------------------------- 표본·결정·봉인


def write_auto_rows(
    root: Path,
    items: list[ar.ReviewItem],
    flagged: set[str] = frozenset(),  # type: ignore[assignment]
) -> list[dict[str, Any]]:
    rows = []
    for it in items:
        for i, fam in enumerate(it.reviewers):
            verdict = dict.fromkeys(it.fields, "ok")
            if it.item_id in flagged and i == 0:
                verdict[it.fields[0]] = "issue"
            rows.append(row_for(it, fam, verdict))
    (root / ar.AUTO_FILE).write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    write_sample_ids(root, sorted(flagged))
    return rows


def write_sample_ids(root: Path, ids: list[str]) -> None:
    """표본 선택 목록(id만)을 쓴다. 기본은 flagged만 표본이다."""
    payload = {
        "schema": "human-sample-v1",
        "seed": 1,
        "items": [{"id": i, "kind": "x", "selected_because": "flagged"} for i in ids],
    }
    (root / ar.SAMPLE_FILE).write_text(json.dumps(payload), encoding="utf-8")


def pick_flagged(items: list[ar.ReviewItem]) -> set[str]:
    tasks = [i.item_id for i in items if i.kind == "task"]
    queries = [i.item_id for i in items if i.kind == "search"]
    return {tasks[0], tasks[50], queries[3]}


def test_sample_includes_all_flagged_plus_ten_percent_ceil(
    work: Path, corpus: ar.Corpus, items: list[ar.ReviewItem]
) -> None:
    flagged = pick_flagged(items)
    rows = write_auto_rows(work, items, flagged)
    sample = ar.build_sample(corpus, rows)
    got = {e["id"]: e for e in sample["items"]}
    assert flagged <= set(got) and all(got[i]["selected_because"] == "flagged" for i in flagged)
    random_ids = [e for e in sample["items"] if e["selected_because"] == "random"]
    tasks_ok = 192 - 2  # 종단 원본 중 flagged가 아닌 것
    queries_ok = 180 - 1
    expected = -(-tasks_ok // 10) + -(-queries_ok // 10)  # 올림
    assert len(random_ids) == expected == 19 + 18
    assert sum(e["kind"] == "task" for e in random_ids) == 19
    first = got[next(iter(flagged & {i.item_id for i in items if i.kind == "task"}))]
    assert first["packet"] and first["auto_review"] and first["type"]
    assert {r["reviewer"] for r in first["auto_review"]} <= {"openai", "anthropic", "google"}


def test_sample_is_seeded_and_deterministic(
    work: Path, corpus: ar.Corpus, items: list[ar.ReviewItem]
) -> None:
    rows = write_auto_rows(work, items, pick_flagged(items))
    a = ar.build_sample(corpus, rows, seed=11)
    b = ar.build_sample(corpus, rows, seed=11)
    c = ar.build_sample(corpus, rows, seed=12)
    assert a == b
    assert {e["id"] for e in a["items"]} != {e["id"] for e in c["items"]}


def test_sample_attaches_fault_derivatives_to_their_origin(
    work: Path, corpus: ar.Corpus, items: list[ar.ReviewItem]
) -> None:
    rows = write_auto_rows(work, items)
    origin = "t-all-day-0001"
    rows = write_auto_rows(work, items, {origin})
    sample = ar.build_sample(corpus, rows)
    entry = next(e for e in sample["items"] if e["id"] == origin)
    assert entry["follows"] == ["t-all-day-0001-fault"]
    assert not any(e["id"].endswith(("-fault", "-para")) for e in sample["items"])


def test_sample_refuses_incomplete_review_unless_allowed(
    work: Path, corpus: ar.Corpus, items: list[ar.ReviewItem]
) -> None:
    rows = write_auto_rows(work, items[:10])
    with pytest.raises(DatasetError, match="끝나지 않은"):
        ar.build_sample(corpus, rows)
    sample = ar.build_sample(corpus, rows, allow_incomplete=True)
    assert sample["counts"]["incomplete"] == len(items) - 10


def test_sample_cli_refuses_output_inside_dataset(work: Path) -> None:
    code = ar.main(["sample", "--dataset-dir", str(work), "--out", str(work / "p.json")])
    assert code == ar.EXIT_USAGE


def test_sample_cli_writes_packet(work: Path, items: list[ar.ReviewItem], tmp_path: Path) -> None:
    write_auto_rows(work, items, pick_flagged(items))
    out = tmp_path / "scratch" / "human-review-packet.json"
    assert ar.main(["sample", "--dataset-dir", str(work), "--out", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["counts"]["flagged"] == 3 and data["items"][0]["packet"]


def decisions_file(path: Path, rows: list[dict[str, str]]) -> Path:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    return path


def test_apply_records_auto_pass_and_inherits_to_derivatives(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    flagged = pick_flagged(items)
    write_auto_rows(work, items, flagged)
    human = sorted(i for i in flagged if not i.startswith("sc-"))
    decisions = decisions_file(
        tmp_path / "d.jsonl",
        [
            {"id": human[0], "decision": "approve", "note": "문제 없음"},
            {"id": human[1], "decision": "approve", "note": ""},
            {
                "id": next(i for i in flagged if i.startswith("sc-")),
                "decision": "approve",
                "note": "",
            },
        ],
    )
    summary = ar.apply_decisions(work, decisions)
    review = read_review(work)
    assert review[human[0]].note == "문제 없음"
    plain = next(i.item_id for i in items if i.kind == "task" and i.item_id not in flagged)
    assert review[plain].decision == "approve" and review[plain].note == ar.AUTO_NOTE
    assert review["t-all-day-0001-fault"].note == "inherited from t-all-day-0001"
    paras = [k for k, v in review.items() if v.note == ar.PARAPHRASE_NOTE]
    assert len(paras) == 24 == len([k for k in review if k.endswith("-para")])
    search = read_search_review(work)
    assert len(search) == 180
    defects = {k for k, v in search.items() if v["note"].startswith("auto: body defect (")}
    assert len(defects) == 36  # 본문 결함 후보 12주제 x 3질의
    assert all(search[k]["decision"] == "exclude" for k in defects)
    assert all(v["decision"] == "approve" for k, v in search.items() if k not in defects)
    assert summary["search"]["auto_excluded_body_defect"] == 36
    assert summary["tasks"]["fix"] == 0 and summary["search"]["fix"] == 0
    assert len(review) == 240


def test_apply_leaves_unreviewed_flagged_items_without_a_record(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    flagged = pick_flagged(items)
    write_auto_rows(work, items, flagged)
    ar.apply_decisions(work, decisions_file(tmp_path / "d.jsonl", []))
    review = read_review(work)
    task_flagged = sorted(i for i in flagged if not i.startswith("sc-"))
    assert not any(t in review for t in task_flagged)
    assert not any(q in read_search_review(work) for q in flagged if q.startswith("sc-"))
    with pytest.raises(ar.SealError, match="검수 기록이 없는"):
        ar.seal_dataset(work)


def test_apply_exclude_propagates_to_fault_and_paraphrase(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    write_auto_rows(work, items)
    decisions = decisions_file(
        tmp_path / "d.jsonl",
        [
            {"id": "t-all-day-0001", "decision": "exclude", "note": "정답 오류"},
            {"id": "t-all-day-0003", "decision": "exclude", "note": "모호"},
        ],
    )
    ar.apply_decisions(work, decisions)
    review = read_review(work)
    assert review["t-all-day-0001-fault"].decision == "exclude"
    assert review["t-all-day-0003-para"].decision == "exclude"
    ids = {t.task_id for t in load_dataset(work).tasks}
    assert not ids & {
        "t-all-day-0001",
        "t-all-day-0001-fault",
        "t-all-day-0003",
        "t-all-day-0003-para",
    }
    assert len(ids) == 240 - 4
    assert len(load_dataset(work, include_excluded=True).tasks) == 240
    # 같은 결정 파일을 다시 적용해도 결과가 같다
    ar.apply_decisions(work, decisions)
    assert read_review(work) == review


def test_apply_search_exclude_is_per_query_and_hidden_on_load(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    write_auto_rows(work, items)
    qid = "sc-search-topic-0001-keyword"
    ar.apply_decisions(
        work,
        decisions_file(
            tmp_path / "d.jsonl", [{"id": qid, "decision": "exclude", "note": "질의가 모호"}]
        ),
    )
    loaded = load_search_set(work / "search.jsonl", work / "search-manifest.json")
    assert qid not in {r["query_id"] for r in loaded} and len(loaded) == 180 - 36 - 1
    everything = load_search_set(work / "search.jsonl", include_excluded=True)
    assert len(everything) == 180
    sibling = "sc-search-topic-0001-person_time"
    assert sibling in {r["query_id"] for r in loaded}


def test_apply_recomputes_test_cluster_count_after_exclusion(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    write_auto_rows(work, items)
    ds = load_dataset(work)
    test_tasks = [t for t in ds.tasks if t.split == "test"]
    base_clusters = len({t.cluster_id for t in test_tasks})
    # 묶음 하나의 test 과제를 전부 제외하면 묶음 수가 하나 줄어든다.
    cluster = test_tasks[0].cluster_id
    members = [t for t in test_tasks if t.cluster_id == cluster]
    origins = [t for t in members if "derived:none" in t.tags]
    assert origins
    decisions = decisions_file(
        tmp_path / "d.jsonl",
        [{"id": t.task_id, "decision": "exclude", "note": "x"} for t in origins],
    )
    summary = ar.apply_decisions(work, decisions)
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    review = manifest["details"]["review"]
    remaining = [
        t
        for t in members
        if t.task_id not in read_review(work) or read_review(work)[t.task_id].decision != "exclude"
    ]
    expected = base_clusters - (0 if remaining else 1)
    assert review["test_clusters"] == expected == summary["tasks"]["test_clusters"]
    assert review["test_tasks"] == len([t for t in load_dataset(work).tasks if t.split == "test"])


def test_apply_rejects_unknown_and_derived_ids(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    write_auto_rows(work, items)
    for bad in ("nope", "t-all-day-0001-fault"):
        with pytest.raises(DatasetError, match="원본 과제나 검색 질의가 아닌"):
            ar.apply_decisions(
                work,
                decisions_file(
                    tmp_path / "d.jsonl", [{"id": bad, "decision": "approve", "note": ""}]
                ),
            )


def test_apply_fix_warns_and_blocks_seal(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    write_auto_rows(work, items)
    summary = ar.apply_decisions(
        work,
        decisions_file(
            tmp_path / "d.jsonl", [{"id": "t-all-day-0001", "decision": "fix", "note": "수정 필요"}]
        ),
    )
    assert summary["tasks"]["fix"] == 2  # 원본과 장애 파생
    assert any("t-all-day-0001" in w for w in summary["tasks"]["warnings"])
    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["details"]["review"]["warnings"]
    with pytest.raises(ar.SealError, match="fix 상태"):
        ar.seal_dataset(work)
    assert "seal" not in json.loads((work / "manifest.json").read_text(encoding="utf-8"))


def test_human_decision_survives_reapply_and_can_be_overridden(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    write_auto_rows(work, items)
    ar.apply_decisions(
        work,
        decisions_file(
            tmp_path / "a.jsonl", [{"id": "t-all-day-0003", "decision": "fix", "note": "고칠 것"}]
        ),
    )
    ar.apply_decisions(work, decisions_file(tmp_path / "b.jsonl", []))
    assert read_review(work)["t-all-day-0003"].decision == "fix"
    ar.apply_decisions(
        work,
        decisions_file(
            tmp_path / "c.jsonl", [{"id": "t-all-day-0003", "decision": "approve", "note": "고침"}]
        ),
    )
    assert read_review(work)["t-all-day-0003"].note == "고침"


FIXED_NOW = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


def applied(work: Path, items: list[ar.ReviewItem], tmp_path: Path) -> None:
    write_auto_rows(work, items)
    ar.apply_decisions(
        work,
        decisions_file(
            tmp_path / "d.jsonl", [{"id": "t-all-day-0001", "decision": "exclude", "note": "x"}]
        ),
    )


def test_seal_writes_hashes_counts_and_summary(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    applied(work, items, tmp_path)
    result = ar.seal_dataset(work, now=lambda: FIXED_NOW)
    seal = json.loads((work / "manifest.json").read_text(encoding="utf-8"))["seal"]
    assert seal == result["tasks"]
    assert seal["sealed_at"] == "2026-11-20T09:00:00+00:00"
    ds = load_dataset(work)
    test = [t for t in ds.tasks if t.split == "test"]
    assert seal["test_items"] == len(test)
    assert seal["test_clusters"] == len({t.cluster_id for t in test})
    assert len(seal["test_sha256"]) == 64
    assert seal["review"]["excluded"] == 2
    assert seal["review"]["self_agreement"] == "미측정, 진행을 막지 않음(ADR-0026 §8)"
    assert seal["review"]["auto_pass"] == 191 and seal["review"]["human_decided"] == 1
    assert seal["review"]["human_sampled"] == 0  # 표본 파일 기준(flagged 없음)
    assert len(seal["world_sha256"]) == 64 and len(seal["skeleton_sha256"]) == 64
    sm = json.loads((work / "search-manifest.json").read_text(encoding="utf-8"))
    assert sm["seal"]["test_items"] == 87 and sm["seal"]["review"]["auto_pass"] == 144
    assert sm["cluster_count"] == 60  # 봉인이 기존 검증 필드를 건드리지 않는다
    assert ar.verify_seal(work) == []
    # 봉인 뒤에도 데이터셋이 그대로 읽힌다
    assert load_dataset(work).manifest.seal == seal
    load_search_set(work / "search.jsonl", work / "search-manifest.json")


def test_seal_hash_is_stable_and_ignores_dev_changes(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    applied(work, items, tmp_path)
    ar.seal_dataset(work, now=lambda: FIXED_NOW)
    first = json.loads((work / "manifest.json").read_text(encoding="utf-8"))["seal"]["test_sha256"]
    ar.seal_dataset(work, now=lambda: FIXED_NOW)
    again = json.loads((work / "manifest.json").read_text(encoding="utf-8"))["seal"]["test_sha256"]
    assert first == again
    rows = [json.loads(x) for x in (work / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]
    dev = next(r for r in rows if r["split"] == "dev")
    dev["turns"][0]["content"] += " (수정)"
    (work / "tasks.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    assert ar.verify_seal(work) == []


def test_verify_seal_catches_test_changes(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    applied(work, items, tmp_path)
    ar.seal_dataset(work, now=lambda: FIXED_NOW)
    rows = [json.loads(x) for x in (work / "tasks.jsonl").read_text(encoding="utf-8").splitlines()]
    victim = next(r for r in rows if r["split"] == "test" and "derived:none" in r["tags"])
    victim["turns"][0]["content"] += " 바꿈"
    (work / "tasks.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    problems = ar.verify_seal(work)
    assert len(problems) == 1 and "종단 과제 test 문항이 봉인 때와 다르다" in problems[0]
    assert "ADR-0010" in problems[0]


def test_verify_seal_catches_search_changes_and_exclusion_changes(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    applied(work, items, tmp_path)
    ar.seal_dataset(work, now=lambda: FIXED_NOW)
    # 봉인 뒤에 test 질의를 제외해도 해시가 달라진다
    test_q = next(r for r in load_search_set(work / "search.jsonl") if r["split"] == "test")
    with (work / "search-review.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps({"query_id": test_q["query_id"], "decision": "exclude", "note": ""}) + "\n"
        )
    problems = ar.verify_seal(work)
    assert len(problems) == 1 and "검색 질의 test 문항이 봉인 때와 다르다" in problems[0]


def test_seal_refuses_without_search_records(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    applied(work, items, tmp_path)
    (work / "search-review.jsonl").unlink()
    with pytest.raises(ar.SealError, match="검색 질의 180개"):
        ar.seal_dataset(work)


def test_loading_never_changes_without_review_files(work: Path) -> None:
    assert len(load_dataset(work).tasks) == 240
    assert len(load_search_set(work / "search.jsonl", work / "search-manifest.json")) == 180


# ---------------------------------------------------------------- 커밋된 데이터셋


def test_committed_dataset_matches_its_seal() -> None:
    """봉인된 뒤 test 문항이 바뀌면 실패한다. 봉인 전(seal 없음)이면 건너뛴다."""
    manifest = load_dataset(COMMITTED, include_excluded=True).manifest
    sm = json.loads((COMMITTED / "search-manifest.json").read_text(encoding="utf-8"))
    if manifest.seal is None and "seal" not in sm:
        pytest.skip("아직 봉인 전이다(auto_review seal을 실행하기 전)")
    assert ar.verify_seal(COMMITTED) == []


# ---------------------------------------------------------------- 검토 지적 반영


def test_exhausted_mismatch_or_malformed_is_aborted_and_retried_on_resume(
    six: list[ar.ReviewItem], tmp_path: Path
) -> None:
    item = six[0]
    for exc_type in (ModelMismatchError, MalformedResponseError):
        out = tmp_path / exc_type.__name__
        out.mkdir()
        llm = FakeLLM(lambda *_, t=exc_type: t("x"))
        summary = run([item], out, llm, cfg())
        assert summary.parse_failed == 0
        rows = ar.read_rows(out / ar.AUTO_FILE)
        assert rows and all(r["status"] == "aborted" and not r["parse_failed"] for r in rows)
        assert ar.item_states([item], rows)[item.item_id].status == "incomplete"
        # 재개하면 정상 응답으로 다시 시도해 확정한다.
        ok = FakeLLM(answer_for({item.item_id: item}))
        run([item], out, ok, cfg())
        final = ar.read_rows(out / ar.AUTO_FILE)
        assert ar.item_states([item], final)[item.item_id].status == "pass"
        assert len(ok.requests) == 2


def test_apply_refuses_without_sample_file(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    write_auto_rows(work, items)
    (work / ar.SAMPLE_FILE).unlink()
    with pytest.raises(DatasetError, match="human-sample.json"):
        ar.apply_decisions(work, decisions_file(tmp_path / "d.jsonl", []))
    with pytest.raises(DatasetError, match="human-sample.json"):
        ar.seal_dataset(work)


def test_sampled_random_item_needs_a_human_decision_before_seal(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    write_auto_rows(work, items)
    task_id, query_id = "t-all-day-0003", "sc-search-topic-0001-keyword"
    write_sample_ids(work, [task_id, query_id])  # 둘 다 자동 검수는 통과했지만 표본이다
    ar.apply_decisions(work, decisions_file(tmp_path / "d.jsonl", []))
    assert task_id not in read_review(work)
    assert query_id not in read_search_review(work)
    with pytest.raises(ar.SealError, match="검수 기록이 없는"):
        ar.seal_dataset(work)
    ar.apply_decisions(
        work,
        decisions_file(
            tmp_path / "e.jsonl",
            [
                {"id": task_id, "decision": "approve", "note": "확인"},
                {"id": query_id, "decision": "approve", "note": "확인"},
            ],
        ),
    )
    result = ar.seal_dataset(work, now=lambda: FIXED_NOW)
    assert result["tasks"]["review"]["human_sampled"] == 1
    assert result["search"]["review"]["human_sampled"] == 1


def test_sample_cli_writes_selection_file_with_ids_only(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    write_auto_rows(work, items, pick_flagged(items))
    (work / ar.SAMPLE_FILE).unlink()
    out = tmp_path / "scratch" / "p.json"
    assert ar.main(["sample", "--dataset-dir", str(work), "--out", str(out), "--seed", "7"]) == 0
    sel = json.loads((work / ar.SAMPLE_FILE).read_text(encoding="utf-8"))
    packet = json.loads(out.read_text(encoding="utf-8"))
    assert sel["seed"] == 7 and len(sel["items"]) == len(packet["items"])
    assert {k for e in sel["items"] for k in e} == {"id", "kind", "selected_because"}
    assert {e["selected_because"] for e in sel["items"]} == {"flagged", "random"}
    assert "packet" not in json.dumps(sel)


def test_verify_seal_catches_world_and_skeleton_changes(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    applied(work, items, tmp_path)
    ar.seal_dataset(work, now=lambda: FIXED_NOW)
    assert ar.verify_seal(work) == []
    for name in ar.BIG_FILES:
        link = work / name
        original = link.resolve()
        link.unlink()
        link.write_bytes(original.read_bytes() + b" ")
        problems = ar.verify_seal(work)
        assert any(name in p for p in problems), name
        link.unlink()
        link.symlink_to(original)
        assert ar.verify_seal(work) == []


def test_sample_cli_refuses_output_inside_a_git_repo(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    write_auto_rows(work, items)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    out = repo / "scratch" / "p.json"
    code = ar.main(["sample", "--dataset-dir", str(work), "--out", str(out)])
    assert code == ar.EXIT_USAGE and not out.exists()
    # 워크트리처럼 .git이 파일이어도 저장소로 본다.
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: x", encoding="utf-8")
    assert (
        ar.main(["sample", "--dataset-dir", str(work), "--out", str(wt / "p.json")])
        == ar.EXIT_USAGE
    )


def test_sample_size_uses_exact_integer_ceiling(
    work: Path, corpus: ar.Corpus, items: list[ar.ReviewItem]
) -> None:
    rows = write_auto_rows(work, items)
    # 종단 원본 192, 검색 180 -> 20 + 18. 부동소수 곱셈이 아니라 정수 올림이다.
    sample = ar.build_sample(corpus, rows)
    assert sample["counts"]["random"] == -(-192 // 10) + -(-180 // 10) == 38
    for num, den in ((3, 10), (1, 20)):
        got = ar.build_sample(corpus, rows, fraction=num / den)
        assert got["counts"]["random"] == -(-192 * num // den) + -(-180 * num // den)


def test_apply_auto_excludes_body_defect_topics_and_human_decisions_win(
    work: Path, items: list[ar.ReviewItem], tmp_path: Path
) -> None:
    write_auto_rows(work, items)
    manifest = json.loads((work / "search-manifest.json").read_text(encoding="utf-8"))
    topic, reason = next(iter(sorted(manifest["exclusion_candidates"].items())))
    keep = f"{topic}-keyword"
    decisions = decisions_file(
        tmp_path / "d.jsonl", [{"id": keep, "decision": "approve", "note": "사람이 확인"}]
    )
    ar.apply_decisions(work, decisions)
    search = read_search_review(work)
    assert search[keep] == {"decision": "approve", "note": "사람이 확인"}
    other = search[f"{topic}-person_time"]
    assert other == {"decision": "exclude", "note": f"auto: body defect ({reason})"}
    # 다시 반영해도 같은 결과이고 사람 결정은 그대로다
    ar.apply_decisions(work, decisions)
    assert read_search_review(work) == search


def test_homonym_packet_shows_interpretation_labels(corpus: ar.Corpus) -> None:
    text = rp.task_packet(corpus.tw, task_named(corpus, "t-ambiguous-0034"))
    sc = next(s for s in corpus.tw.skeleton.scenarios if s.scenario_id == "sc-ambiguous-0034")
    for interp in sc.truth.interpretations:  # type: ignore[union-attr]
        assert f"[{interp.label}]" in text


def test_person_time_search_packet_shows_sender_and_owner(corpus: ar.Corpus) -> None:
    row = next(q for q in corpus.queries if q["form"] == "person_time")
    other = next(q for q in corpus.queries if q["form"] == "keyword")
    assert "@" in rp.search_packet(corpus.tw, row) or "소유자" in rp.search_packet(corpus.tw, row)
    assert "소유자" not in rp.search_packet(corpus.tw, other)
