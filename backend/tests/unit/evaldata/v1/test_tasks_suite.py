"""종단 과제·스모크 생성기 테스트 (ADR-0026 §4·§5): 쿼터, 분할 묶음, 중복 제거, 스모크, 결정성, CLI.
커밋된 세계 v1로 한 번 만든 결과를 검사한다. 표현 변형은 스크립트 LLM으로만 확인한다."""

import hashlib
import json
import random
import shutil
from collections import Counter
from dataclasses import replace
from datetime import UTC
from pathlib import Path

import pytest

from eval_tasks_support import (
    COMMITTED_V1,
    SEED,
    real_candidates,
    real_suite,
    real_task_world,
    slim_dataset,
)
from orbit.evaldata import dataset as ds
from orbit.evaldata.schema import EventSetAnswer, EventTimeAnswer, Manifest, Task
from orbit.evaldata.v1 import paraphrase as pp
from orbit.evaldata.v1.realize_models import FamilyModel
from orbit.evaldata.v1.task_gold import Draft
from orbit.evaldata.v1.tasks import (
    DEFAULT_PLAN,
    SuiteError,
    assign_splits,
    build_suite,
    clusters_of,
    fingerprint,
    group_of,
    main,
    manifest_for,
    pick_balanced,
    run,
    select_originals,
    select_smoke,
)
from orbit.evalrunner.task_source import load_dataset as load_runner_dataset
from orbit.llm.types import LLMRequest, LLMResponse, LLMUsage

EXPECTED_ORIGINALS = {
    "calendar": 32,
    "mail": 40,
    "write": 20,
    "multi_turn": 20,
    "document": 16,
    "reminder": 16,
    "mismatch": 16,
    "unanswerable": 16,
    "ambiguous": 16,
}


def tasks() -> list[Task]:
    return real_suite().tasks


def kind(t: Task) -> str:
    return next(x for x in t.tags if x.startswith("type:"))[5:]


def derived(t: Task) -> str:
    return next(x for x in t.tags if x.startswith("derived:"))[8:]


def originals() -> list[Task]:
    return [t for t in tasks() if derived(t) == "none"]


# ---------------------------------------------------------------- 쿼터


def test_originals_match_the_quota_per_type() -> None:
    assert dict(Counter(kind(t) for t in originals())) == EXPECTED_ORIGINALS
    assert len(originals()) == 192
    assert sum(1 for t in tasks() if derived(t) == "fault") == 24
    assert len(tasks()) == 216  # 표현 변형 24개는 LLM을 부를 때만 더해진다


def test_each_original_comes_from_a_different_scenario() -> None:
    scenarios = [t.scenario_id for t in originals()]
    assert len(scenarios) == len(set(scenarios))


def test_users_and_families_are_spread_and_la_user_is_included() -> None:
    users = Counter(t.account_id for t in originals())
    assert set(users) == {f"acct-u{i}" for i in range(1, 7)}
    assert max(users.values()) - min(users.values()) <= 8
    la = [t for t in originals() if t.account_id == "acct-u6"]
    assert len({kind(t) for t in la}) >= 6 and all(t.time_zone == "America/Los_Angeles" for t in la)
    families = Counter(next(x for x in t.tags if x.startswith("family:")) for t in originals())
    assert set(families) == {"family:openai", "family:anthropic", "family:google"}
    assert max(families.values()) - min(families.values()) <= 10
    by_type: dict[str, Counter[str]] = {}
    for t in originals():
        fam = next(x for x in t.tags if x.startswith("family:"))
        by_type.setdefault(kind(t), Counter())[fam] += 1
    for k, c in by_type.items():
        assert max(c.values()) - min(c.values()) <= 4, (k, c)
    by_user: dict[str, Counter[str]] = {}
    for t in originals():
        by_user.setdefault(kind(t), Counter())[t.account_id] += 1
    for k, c in by_user.items():
        assert len(c) == 6 and max(c.values()) - min(c.values()) <= 2, (k, c)


def test_every_scenario_variant_in_the_quota_is_present() -> None:
    variants = Counter(
        next((x for x in t.tags if x.startswith("variant:")), "") for t in originals()
    )
    for v in (
        "move",
        "cancel",
        "meeting_request",
        "reply_needed",
        "person_no_meeting",
        "no_document",
        "no_amount",
        "project_no_meeting",
        "person_meetings",
        "same_project_notes",
        "homonym_person",
        "doc_fact",
        "doc_version",
        "request",
        "ask",
    ):
        assert variants[f"variant:{v}"] > 0, v  # fmt: skip
    assert variants["variant:request"] == 12 and variants["variant:ask"] == 4
    assert {
        f"variant:{k}" for k in ("deadline", "amount", "place", "decision", "attachment")
    } <= set(variants)


# ---------------------------------------------------------------- 쓰기·거절·장애 파생


def test_reject_tasks_expect_no_change_and_the_rest_expect_one() -> None:
    writes = [t for t in originals() if kind(t) == "write"]
    rejects = [t for t in writes if t.approval_policy == ["reject"]]
    assert len(rejects) == 4 and "approval:reject" in rejects[0].tags
    assert all(t.gold.expected_changes == [] for t in rejects)
    assert all(len(t.gold.expected_changes or []) == 1 for t in writes if t not in rejects)
    assert {c.kind for t in writes for c in t.gold.expected_changes or []} == {
        "event_created", "event_updated", "event_cancelled", "draft_sent",
    }  # fmt: skip


def test_fault_tasks_keep_the_gold_and_share_the_cluster() -> None:
    by_id = {t.task_id: t for t in tasks()}
    faults = [t for t in tasks() if derived(t) == "fault"]
    errors = Counter(t.fault_plan["rules"][0]["error"] for t in faults if t.fault_plan)
    assert errors["result_unknown"] == 8
    assert errors["transient"] >= 6 and errors["rate_limited"] >= 6
    assert sum(errors.values()) == 24
    for f in faults:
        origin = by_id[f.task_id.removesuffix("-fault")]
        assert f.gold == origin.gold and f.turns == origin.turns
        assert f.cluster_id == origin.cluster_id and f.split == origin.split
        assert (
            f.scenario_id == origin.scenario_id and f.difficulty["derived_from"] == origin.task_id
        )
        (rule,) = f.fault_plan["rules"]  # type: ignore[index]
        assert rule["nth_call"] == 1
        if rule["error"] == "result_unknown":
            assert origin.gold.expected_changes and origin.approval_policy is None
            assert rule["tool"] in ("create_event", "update_event", "cancel_event", "send_draft")
        else:
            assert rule["tool"] in ("list_events", "search_messages")
            assert not origin.gold.expected_changes


# ---------------------------------------------------------------- 분할·묶음


def test_dataset_validates_and_clusters_never_straddle_splits() -> None:
    tw, world = real_task_world()
    manifest = manifest_for(tw, world, real_suite(), SEED, "test-sha", {"enabled": False})
    assert ds.validate_dataset(manifest, world, tasks()) == []
    by_cluster: dict[str, set[str]] = {}
    for t in tasks():
        by_cluster.setdefault(t.cluster_id, set()).add(t.split)
    assert all(len(s) == 1 for s in by_cluster.values())
    for group in ds.evidence_clusters(tasks()):  # 근거를 공유하는 과제는 한 묶음이다
        assert len({t.cluster_id for t in group}) == 1


def test_test_split_has_at_least_100_independent_clusters_and_both_splits_per_type() -> None:
    test_clusters = {t.cluster_id for t in tasks() if t.split == "test"}
    assert len(test_clusters) >= 100
    n_test = sum(1 for t in tasks() if t.split == "test")
    assert abs(n_test - round(len(tasks()) * 7 / 12)) <= DEFAULT_PLAN.test_tolerance
    per: dict[str, Counter[str]] = {}
    for t in tasks():
        per.setdefault(group_of(t), Counter())[t.split] += 1
    assert set(per) == set(EXPECTED_ORIGINALS) | {"fault"}
    for g, c in per.items():
        assert c["dev"] > 0 and c["test"] > 0, g
        assert abs(c["test"] - (c["dev"] + c["test"]) * 7 / 12) <= 2.5, (g, dict(c))


def test_sibling_tasks_of_one_scenario_are_one_cluster() -> None:
    by_scenario: dict[str, set[str]] = {}
    for t in tasks():
        by_scenario.setdefault(t.scenario_id, set()).add(t.cluster_id)
    assert all(len(v) == 1 for v in by_scenario.values())


def test_assign_splits_is_deterministic_and_respects_min_clusters() -> None:
    clusters = clusters_of(tasks())
    a = assign_splits(clusters, DEFAULT_PLAN, SEED)
    b = assign_splits(clusters, DEFAULT_PLAN, SEED)
    assert a == b and len(a.test_clusters) >= DEFAULT_PLAN.min_test_clusters
    other = assign_splits(clusters, DEFAULT_PLAN, SEED + 1)
    assert len(other.test_clusters) >= DEFAULT_PLAN.min_test_clusters
    assert other.test_clusters != a.test_clusters


# ---------------------------------------------------------------- 중복 제거·근거 비공유


def test_no_two_originals_share_evidence_or_an_answer_fingerprint() -> None:
    chosen, _ = select_originals(real_candidates(), DEFAULT_PLAN, SEED)
    prints = [fingerprint(o.draft) for o in chosen]
    assert len(prints) == len(set(prints)) == 192
    seen: dict[str, str] = {}
    for o in chosen:
        from orbit.evaldata.v1.task_gold import ref_tags

        assert set(ref_tags(o.draft.scenario.evidence)) <= set(o.draft.refs)
        for ref in o.draft.refs:
            assert ref not in seen, (ref, seen[ref], o.task_id)
            seen[ref] = o.task_id


def test_fingerprint_ignores_the_question_and_splits_by_account() -> None:
    d = next(d for d in real_candidates().drafts.values() if d.scenario.type == "day_agenda")
    twin = Draft(d.scenario, d.kind, d.form, ["다른 질문 문장"], d.gold, d.refs)
    assert fingerprint(d) == fingerprint(twin)
    other_account = d.scenario.model_copy(update={"account_id": "acct-u2"})
    moved = Draft(other_account, d.kind, d.form, d.turns, d.gold, d.refs)
    assert fingerprint(moved) != fingerprint(d)
    reject = Draft(d.scenario, d.kind, d.form, d.turns, d.gold, d.refs, fingerprint_extra=1)
    assert fingerprint(reject) != fingerprint(d)


def test_fingerprint_normalizes_sets_and_instants_and_ignores_auxiliary_facts() -> None:
    day = next(d for d in real_candidates().drafts.values() if d.scenario.type == "day_agenda")
    answer = day.gold.expected_answer
    assert isinstance(answer, EventSetAnswer)
    reversed_answer = answer.model_copy(update={"events": list(reversed(answer.events))})
    gold = day.gold.model_copy(update={"expected_answer": reversed_answer, "answer_facts": []})
    assert fingerprint(day) == fingerprint(replace(day, gold=gold))
    timed = next(d for d in real_candidates().drafts.values() if d.scenario.type == "tz_event")
    moment = timed.gold.expected_answer
    assert isinstance(moment, EventTimeAnswer)
    utc = moment.model_copy(update={"start": moment.start.astimezone(UTC)})
    assert fingerprint(timed) == fingerprint(
        replace(timed, gold=timed.gold.model_copy(update={"expected_answer": utc}))
    )


def _pool(scenario_type: str, form: str = "single") -> list[Draft]:
    return [
        d
        for (_, f), d in real_candidates().drafts.items()
        if d.scenario.type == scenario_type and f == form
    ]


def test_pick_balanced_spreads_users_and_stops_at_conflicts() -> None:
    from orbit.evaldata.v1.tasks import Balance, SelectStats, Taken

    pool = _pool("mail_fact")
    picked = pick_balanced(pool, 12, random.Random(1), Balance(), "mail", "s", None)  # noqa: S311  재현 가능한 표본 선택
    users = Counter(d.scenario.account_id for d in picked)
    assert len(users) == 6 and set(users.values()) == {2}
    taken, stats = Taken(), SelectStats()
    first = pick_balanced(pool, 1, random.Random(2), Balance(), "mail", "s", taken, stats)  # noqa: S311  재현 가능한 표본 선택
    twin = Draft(
        pool[0].scenario.model_copy(update={"scenario_id": "sc-twin"}),
        "mail", "single", ["질문"], pool[0].gold, first[0].refs,
    )  # fmt: skip
    with pytest.raises(SuiteError):
        pick_balanced([twin], 1, random.Random(3), Balance(), "mail", "s", taken, stats)  # noqa: S311  재현 가능한 표본 선택
    assert "sc-twin" in stats.ref_conflicts


def test_pick_balanced_fails_with_a_reason_when_the_pool_is_too_small() -> None:
    from orbit.evaldata.v1.tasks import Balance

    with pytest.raises(SuiteError, match="채우지 못했다"):
        pick_balanced(_pool("all_day"), 99, random.Random(1), Balance(), "calendar", "x", None)  # noqa: S311  재현 가능한 표본 선택


def test_unfillable_quota_fails_generation_instead_of_shrinking() -> None:
    plan = DEFAULT_PLAN.__class__(
        **{
            **DEFAULT_PLAN.__dict__,
            "slots": (DEFAULT_PLAN.slots[0].__class__("all_day", "single", 50),),
        }
    )
    with pytest.raises(SuiteError):
        select_originals(real_candidates(), plan, SEED)


def test_too_few_test_clusters_fail_the_split_check() -> None:
    tw, _ = real_task_world()
    plan = DEFAULT_PLAN.__class__(**{**DEFAULT_PLAN.__dict__, "min_test_clusters": 500})
    suite, _ = build_suite(tw, SEED, plan, include_paraphrase=False, candidates=real_candidates())
    assert any("독립 묶음" in p for p in suite.problems)


# ---------------------------------------------------------------- 스모크


def test_smoke_has_25_dev_tasks_covering_every_type() -> None:
    suite = real_suite()
    by_id = {t.task_id: t for t in suite.tasks}
    assert len(suite.smoke_ids) == 25 == len(set(suite.smoke_ids))
    assert all(by_id[i].split == "dev" for i in suite.smoke_ids)
    groups = {group_of(by_id[i]) for i in suite.smoke_ids}
    assert groups == set(EXPECTED_ORIGINALS) | {"fault"}
    assert suite.smoke_ids == sorted(suite.smoke_ids)
    assert select_smoke(suite.tasks, 25, SEED) == suite.smoke_ids
    assert select_smoke(suite.tasks, 25, SEED + 1) != suite.smoke_ids


def test_smoke_never_picks_more_than_the_dev_tasks_and_prefers_distinct_clusters() -> None:
    dev = [t for t in tasks() if t.split == "dev"]
    assert len(select_smoke(tasks(), 10_000, SEED)) == len(dev)
    picked = select_smoke(tasks(), 25, SEED)
    by_id = {t.task_id: t for t in tasks()}
    assert len({by_id[i].cluster_id for i in picked}) >= 20


def test_runner_smoke_subset_contains_only_manifest_ids_and_preserves_test_seal() -> None:
    from orbit.evalrunner.cli import _smoke_subset

    _, world = real_task_world()
    suite = real_suite()
    dataset = slim_dataset(suite.tasks, world)
    dataset = replace(dataset, manifest={"smoke_task_ids": suite.smoke_ids})
    smoke = _smoke_subset(dataset)
    assert {t.task_id for t in smoke.tasks} == set(suite.smoke_ids)
    assert len(smoke.tasks) == 25 and all(t.split == "dev" for t in smoke.tasks)
    test_task = next(t for t in suite.tasks if t.split == "test")
    with pytest.raises(ds.DatasetError, match="봉인"):
        _smoke_subset(replace(dataset, manifest={"smoke_task_ids": [test_task.task_id]}))
    with pytest.raises(ds.DatasetError, match="없는 과제"):
        _smoke_subset(replace(dataset, manifest={"smoke_task_ids": ["not-a-task"]}))
    with pytest.raises(ds.DatasetError, match="목록이 없는"):
        _smoke_subset(replace(dataset, manifest={}))


# ---------------------------------------------------------------- 결정성


def test_building_twice_gives_the_same_tasks() -> None:
    tw, _ = real_task_world()
    again, _ = build_suite(tw, SEED, DEFAULT_PLAN, include_paraphrase=False)
    assert [t.model_dump(mode="json") for t in again.tasks] == [
        t.model_dump(mode="json") for t in tasks()
    ]
    assert again.smoke_ids == real_suite().smoke_ids
    assert again.details == real_suite().details


def test_different_seed_changes_the_selection() -> None:
    tw, _ = real_task_world()
    other, _ = build_suite(tw, SEED + 1, DEFAULT_PLAN, include_paraphrase=False)
    assert {t.task_id for t in other.tasks} != {t.task_id for t in tasks()}


# ---------------------------------------------------------------- CLI·출력


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_cli_skip_paraphrase_writes_a_loadable_dataset(tmp_path: Path) -> None:
    out = tmp_path / "v1"
    args = ["--dataset-dir", str(COMMITTED_V1), "--seed", str(SEED), "--skip-paraphrase",
            "--out", str(out), "--git-sha", "test-sha"]  # fmt: skip
    assert main(args) == 0
    loaded = ds.load_dataset(out)
    assert len(loaded.tasks) == 216
    manifest = loaded.manifest
    assert (manifest.name, manifest.version, manifest.created_with) == (
        "orbit-eval", "v1", "structure+llm",
    )  # fmt: skip
    assert manifest.generator_git_sha == "test-sha" and manifest.seed == SEED
    assert manifest.counts.tasks == 216 and manifest.counts.users == 9
    details = manifest.details
    assert details is not None
    assert details["world_sha256"] == digest(COMMITTED_V1 / "world.json")
    assert details["skeleton_sha256"] == digest(COMMITTED_V1 / "skeleton.json")
    assert details["clusters"]["test"] >= 100 and details["paraphrase"] == {
        "enabled": False,
        "note": "--skip-paraphrase: 표현 변형 파생 문항 없이 생성했다",
    }
    assert (
        details["tasks_by_type"]["fault"]["dev"] + details["tasks_by_type"]["fault"]["test"] == 24
    )
    assert manifest.smoke_task_ids == real_suite().smoke_ids
    assert (out / "world.json").exists() and not (out / pp.PARAPHRASE_FILE).exists()
    runner = load_runner_dataset(out)  # 러너가 읽는 경로로도 열린다
    assert runner.manifest["smoke_task_ids"] and len(runner.tasks_for("dev")) > 0
    from orbit.evalrunner.cli import SEAL_FLAG
    from orbit.evalrunner.cli import main as runner_main

    result = tmp_path / "smoke.jsonl"
    cli_args = [
        "run",
        "--dataset",
        str(out),
        "--split",
        "dev",
        "--subset",
        "smoke",
        "--sut",
        "noop",
        "--repeats",
        "1",
        "--out",
        str(result),
    ]
    assert runner_main(cli_args) == 0
    rows = [json.loads(line) for line in result.read_text().splitlines()]
    assert len(rows) == 25 and {row["task_id"] for row in rows} == set(
        manifest.smoke_task_ids or []
    )
    forbidden_out = tmp_path / "test-smoke.jsonl"
    test_args = cli_args[:]
    test_args[test_args.index("--split") + 1] = "test"
    test_args[test_args.index("--out") + 1] = str(forbidden_out)
    assert runner_main([*test_args, SEAL_FLAG]) == 2
    assert not forbidden_out.exists()
    first = (digest(out / "tasks.jsonl"), digest(out / "manifest.json"))
    again = tmp_path / "again"
    assert main([*args[:-4], "--out", str(again), "--git-sha", "test-sha"]) == 0
    assert (digest(again / "tasks.jsonl"), digest(again / "manifest.json")) == first


def test_cli_needs_a_cost_cap_unless_paraphrase_is_skipped(tmp_path: Path) -> None:
    assert main(["--dataset-dir", str(COMMITTED_V1), "--seed", "1", "--out", str(tmp_path)]) == 2
    assert not list(tmp_path.iterdir())
    assert main(["--dataset-dir", str(COMMITTED_V1), "--seed", "1", "--out", str(tmp_path),
                 "--max-cost-usd", "0"]) == 2  # fmt: skip


@pytest.mark.parametrize("cap", ["nan", "inf", "-inf"])
def test_nonfinite_cap_is_rejected_before_generation(tmp_path: Path, cap: str) -> None:
    assert (
        main(
            [
                "--dataset-dir",
                str(COMMITTED_V1),
                "--seed",
                "1",
                "--out",
                str(tmp_path),
                f"--max-cost-usd={cap}",
            ]
        )
        == 2
    )
    assert not list(tmp_path.iterdir())


def clean_input(base: Path) -> Path:
    """커밋된 paraphrase.jsonl 캐시 없이 world.json·skeleton.json만 가진 입력 폴더."""
    input_dir = base / "clean_input"
    if not input_dir.exists():
        input_dir.mkdir()
        for name in ("world.json", "skeleton.json"):
            shutil.copyfile(COMMITTED_V1 / name, input_dir / name)
    return input_dir


def test_cli_dry_run_prints_an_estimate_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--dataset-dir", str(clean_input(tmp_path)), "--seed", str(SEED),
                 "--max-cost-usd", "1", "--dry-run", "--out", str(tmp_path / "x")])  # fmt: skip
    assert code == 0
    text = capsys.readouterr().out
    assert "예상 합계" in text and "필요 24개 중 확보 0개" in text
    assert not (tmp_path / "x").exists()


def test_dry_run_with_saved_paraphrases_does_not_copy_or_call_llm(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    for name in ("world.json", "skeleton.json"):
        (input_dir / name).symlink_to(COMMITTED_V1 / name)
    # A failed cached attempt is enough to exercise the read-only path.
    from orbit.evaldata.v1.realize_models import default_models

    row = {
        "origin_id": "unrelated",
        "status": "failed",
        "family": "openai",
        "cost_usd": 0.01,
        "model_config": default_models()["openai"].manifest(),
    }
    cache = input_dir / pp.PARAPHRASE_FILE
    cache.write_text(json.dumps(row) + "\n", encoding="utf-8")
    before = cache.read_bytes()
    out = tmp_path / "dry"

    def forbidden_factory(model: FamilyModel) -> RewriteLLM:
        raise AssertionError("dry-run에서 LLM을 만들면 안 된다")

    assert (
        run(
            input_dir,
            SEED,
            out,
            skip_paraphrase=False,
            max_cost_usd=1.0,
            dry_run=True,
            sha="test-sha",
            llm_factory=forbidden_factory,
            echo=lambda _: None,
        )
        == 0
    )
    assert not out.exists() and cache.read_bytes() == before


# ---------------- 표현 변형 전체 생성(스크립트 LLM)


class RewriteLLM:
    """원래 질문 뒤에 말을 붙여 돌려준다. 보존 표현이 그대로 들어 있다."""

    def __init__(self, fail_for: set[str] | None = None, cost: float | None = None) -> None:
        self.fail_for = fail_for or set()
        self.cost = cost
        self.requests: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        user = str(request.messages[1].content)
        original = user.splitlines()[0].removeprefix("원래 질문: ")
        original = original.replace("? ", ", ").replace("! ", ", ")
        text = (
            "슬랙으로 알려줘"
            if request.metadata["origin_id"] in self.fail_for
            else (f"{original.replace('. ', ', ').rstrip('.?!')} 부탁해")
        )
        return LLMResponse(
            text=text, tool_calls=(), finish_reason="stop",
            usage=LLMUsage(input_tokens=500, output_tokens=40), cost_usd=self.cost,
            latency_ms=1, model_id="m", provider=None,
        )  # fmt: skip


def full_run(out: Path, llm: RewriteLLM | None, *, cap: float = 1.0) -> int:
    def factory(model: FamilyModel) -> RewriteLLM:
        if llm is None:
            raise AssertionError("예상하지 않은 LLM 생성")
        return llm

    return run(
        clean_input(out.parent), SEED, out, skip_paraphrase=False, max_cost_usd=cap, dry_run=False,
        sha="test-sha", llm_factory=factory, echo=lambda _: None,
    )  # fmt: skip


def test_paraphrase_tasks_are_added_resumed_and_deterministic(tmp_path: Path) -> None:
    out = tmp_path / "full"
    llm = RewriteLLM(fail_for={"t-calendar-never"})
    assert full_run(out, llm) == 0
    loaded = ds.load_dataset(out)
    all_tasks = loaded.tasks
    assert len(all_tasks) == 240
    paras = [t for t in all_tasks if derived(t) == "paraphrase"]
    assert len(paras) == 24
    by_id = {t.task_id: t for t in all_tasks}
    for p in paras:
        origin = by_id[p.task_id.removesuffix("-para")]
        assert p.gold == origin.gold and p.cluster_id == origin.cluster_id
        assert p.turns != origin.turns and p.turns[0].content.endswith("부탁해")
        assert len(origin.turns) == 1 and kind(origin) in (
            "calendar",
            "mail",
            "mismatch",
            "document",
        )
    assert abs(sum(1 for t in all_tasks if t.split == "test") - 140) <= DEFAULT_PLAN.test_tolerance
    assert len({t.cluster_id for t in all_tasks if t.split == "test"}) >= 100
    info = loaded.manifest.details["paraphrase"]  # type: ignore[index]
    assert info["used"] == 24 and info["enabled"] is True and info["cost_usd"] > 0
    assert set(info["models"]) == {"openai", "anthropic", "google"}
    assert info["models"]["anthropic"]["temperature"] is None
    assert Counter(derived(t) for t in all_tasks) == {"none": 192, "fault": 24, "paraphrase": 24}
    bytes_before = (digest(out / "tasks.jsonl"), digest(out / "manifest.json"))
    # 저장된 paraphrase.jsonl만으로 다시 만든다: LLM을 부르지 않는다
    silent = RewriteLLM()
    assert full_run(out, silent) == 0 and silent.requests == []
    assert (digest(out / "tasks.jsonl"), digest(out / "manifest.json")) == bytes_before
    # 응답 원문이 아니라 검증을 통과한 문장만 파일에 있다
    rows = pp.read_all_rows(out / pp.PARAPHRASE_FILE)
    assert len(rows) >= 24 and all(r["status"] in ("ok", "failed") for r in rows)


def test_paraphrase_failures_are_replaced_by_other_originals(tmp_path: Path) -> None:
    probe = tmp_path / "probe"
    llm = RewriteLLM()
    assert full_run(probe, llm) == 0
    first_origin = pp.read_all_rows(probe / pp.PARAPHRASE_FILE)[0]["origin_id"]
    out = tmp_path / "fail"
    bad = RewriteLLM(fail_for={first_origin})
    assert full_run(out, bad) == 0
    loaded = ds.load_dataset(out)
    ids = {t.task_id for t in loaded.tasks}
    assert first_origin + "-para" not in ids
    assert sum(1 for t in loaded.tasks if derived(t) == "paraphrase") == 24
    info = loaded.manifest.details["paraphrase"]  # type: ignore[index]
    assert info["failed_candidates"] == 1


def test_cost_cap_stops_generation_without_writing_tasks(tmp_path: Path) -> None:
    out = tmp_path / "capped"
    llm = RewriteLLM(cost=0.4)
    assert full_run(out, llm, cap=0.5) == 3
    assert not (out / "tasks.jsonl").exists() and (out / pp.PARAPHRASE_FILE).exists()
    assert len(llm.requests) <= 4  # 동시 요청 몇 건 뒤에는 새 요청을 하지 않는다
    # 같은 명령(상한을 올림)으로 이어서 실행하면 끝난다
    resumed = RewriteLLM()
    assert full_run(out, resumed, cap=5.0) == 0
    assert len(ds.load_dataset(out).tasks) == 240


def test_v0_manifest_bytes_are_unchanged_by_the_optional_fields(tmp_path: Path) -> None:
    v0 = COMMITTED_V1.parent / "v0" / "manifest.json"
    manifest = Manifest.model_validate_json(v0.read_text(encoding="utf-8"))
    assert manifest.smoke_task_ids is None and manifest.details is None
    ds.write_manifest(tmp_path, manifest)
    assert (tmp_path / "manifest.json").read_text(encoding="utf-8") == v0.read_text(
        encoding="utf-8"
    )
    assert json.loads(v0.read_text(encoding="utf-8"))["created_with"] == "templates"


def test_committed_dataset_regenerates_from_its_paraphrase_cache_without_llm(
    tmp_path: Path,
) -> None:
    committed = ds.load_dataset(COMMITTED_V1, include_excluded=True)
    assert len(committed.tasks) == 240
    decisions = [
        json.loads(line)
        for line in (COMMITTED_V1 / "review.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    excluded = {d["task_id"] for d in decisions if d["decision"] == "exclude"}
    assert excluded <= {t.task_id for t in committed.tasks}
    default_ids = {t.task_id for t in ds.load_dataset(COMMITTED_V1).tasks}
    assert len(default_ids) == 240 - len(excluded)
    assert default_ids.isdisjoint(excluded)
    assert Counter(derived(t) for t in committed.tasks)["paraphrase"] == 24
    assert (COMMITTED_V1 / pp.PARAPHRASE_FILE).exists()
    out = tmp_path / "regen"

    def forbidden_factory(model: FamilyModel) -> RewriteLLM:
        raise AssertionError("커밋된 캐시로 재생성할 때 LLM을 만들면 안 된다")

    assert (
        run(
            COMMITTED_V1, SEED, out, skip_paraphrase=False, max_cost_usd=1.0, dry_run=False,
            sha="test-sha", llm_factory=forbidden_factory, echo=lambda _: None,
        )
        == 0
    )  # fmt: skip
    assert digest(out / "tasks.jsonl") == digest(COMMITTED_V1 / "tasks.jsonl")
