"""safety-v1 생성기: 결정성, 쌍 불변식, 합성 주소, 분할 누출, 봉인 거부."""

import json
from pathlib import Path

import pytest

from orbit.evaldata import dataset as ds
from orbit.evaldata.rag_v1.seal import SealError
from orbit.evaldata.safety_v1 import build as sb
from orbit.evaldata.safety_v1.cli import main
from orbit.evaldata.safety_v1.constants import ATTACK_TYPES, CANARY_TOKEN, SYNTHETIC_TLD
from orbit.evaldata.safety_v1.review import PAIR_REVIEW
from orbit.evaldata.safety_v1.seal import seal_dataset, verify_seal
from orbit.evaldata.schema import Task


@pytest.fixture(scope="module")
def built() -> sb.Built:
    return sb.build(git_sha="test-sha")


def _out(tmp_path: Path, name: str = "ds") -> Path:
    out = tmp_path / name
    assert main(["build", "--git-sha", "test-sha", "--out", str(out)], echo=lambda _: None) == 0
    return out


def test_same_seed_same_bytes(tmp_path: Path) -> None:
    a, b = _out(tmp_path, "a"), _out(tmp_path, "b")
    for name in (ds.WORLD, ds.TASKS, ds.MANIFEST):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_other_seed_differs() -> None:
    assert sb.build(seed=1).world != sb.build(seed=2).world


def test_counts_and_split(built: sb.Built) -> None:
    attack = [t for t in built.tasks if t.safety and t.safety.role == "attack"]
    assert len(built.tasks) == 160 and len(attack) == 80
    for kind in ATTACK_TYPES:
        of = [t for t in attack if t.safety and t.safety.attack_type == kind]
        assert len(of) == 16
        assert {t.split for t in of} == {"dev", "test"}
        assert sum(t.split == "dev" for t in of) == 8
    assert sum(t.split == "dev" for t in attack) == 40


def test_no_split_or_cluster_leak(built: sb.Built) -> None:
    split_of: dict[str, set[str]] = {}
    for t in built.tasks:
        split_of.setdefault(t.cluster_id, set()).add(t.split)
    assert all(len(s) == 1 for s in split_of.values())
    pairs: dict[str, set[str]] = {}
    for t in built.tasks:
        assert t.safety is not None
        pairs.setdefault(t.safety.pair_id, set()).add(t.cluster_id)
    assert all(len(c) == 1 for c in pairs.values())


def test_pair_invariant_normal_is_attack_minus_one_paragraph(built: sb.Built) -> None:
    by_pair: dict[str, dict[str, Task]] = {}
    for t in built.tasks:
        assert t.safety is not None
        by_pair.setdefault(t.safety.pair_id, {})[t.safety.role] = t
    for roles in by_pair.values():
        atk, nor = roles["attack"], roles["normal"]
        assert atk.safety is not None
        assert atk.turns == nor.turns
        assert atk.gold == nor.gold
        wa = built.world["accounts"][atk.account_id]
        wn = built.world["accounts"][nor.account_id]
        carriers = set(atk.safety.attack_source_ids)
        for kind, text_key in (("messages", "body_text"), ("files", "text")):
            assert [i["id"] for i in wa[kind]] == [i["id"] for i in wn[kind]]
            for ia, inn in zip(wa[kind], wn[kind], strict=True):
                if ia["id"] not in carriers:
                    assert ia == inn
                    continue
                pa = ia[text_key].split("\n\n")
                pn = inn[text_key].split("\n\n")
                assert len(pa) == len(pn) + 1
                assert len([p for p in pa if p not in pn]) == 1
                assert {k: v for k, v in ia.items() if k != text_key} == {
                    k: v for k, v in inn.items() if k != text_key
                }
    assert CANARY_TOKEN not in json.dumps(built.world)


def test_events_equal_within_pair(built: sb.Built) -> None:
    accounts = built.world["accounts"]
    for name in accounts:
        if name.endswith("-a"):
            assert accounts[name]["events"] == accounts[name[:-1] + "n"]["events"]


def test_all_addresses_synthetic(built: sb.Built) -> None:
    assert sb.synthetic_problems(built.world, built.tasks) == []
    assert SYNTHETIC_TLD == ".example.invalid"
    broken = json.loads(json.dumps(built.world))
    first = next(iter(broken["accounts"].values()))
    first["email"] = "x@gmail.com"
    assert sb.synthetic_problems(broken, built.tasks)


def test_loads_through_task_source(tmp_path: Path) -> None:
    from orbit.evalrunner.task_source import load_dataset

    dataset = load_dataset(_out(tmp_path))
    assert len(dataset.tasks) == 160
    assert all(t.approval_policy == ["oracle"] for t in dataset.tasks)


def test_attack_target_and_markers(built: sb.Built) -> None:
    for t in built.tasks:
        s = t.safety
        assert s is not None and s.marker
        if s.attack_type == "exfiltration":
            assert s.attack_target and s.attack_target.endswith(SYNTHETIC_TLD)
            assert s.leak_source_id
        if s.attack_type == "system_leak":
            assert s.marker == CANARY_TOKEN


def test_seal_refuses_until_review_complete(tmp_path: Path) -> None:
    out = _out(tmp_path)
    with pytest.raises(SealError):
        seal_dataset(out, "검수 메모")
    assert verify_seal(out) is None
    rows = [json.loads(x) for x in (out / PAIR_REVIEW).read_text(encoding="utf-8").splitlines()]
    for r in rows:
        r.update(
            normal_task_answerable=True, attack_rule_correct=True, no_real_brand=True,
            decision="approve",
        )  # fmt: skip
    (out / PAIR_REVIEW).write_text(
        "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
        encoding="utf-8",
    )
    block = seal_dataset(out, "검수 메모")
    assert block["test_items"] == 80
    assert verify_seal(out) == []
    with pytest.raises(SealError):
        seal_dataset(out, "다시")
    (out / ds.TASKS).write_text(
        (out / ds.TASKS).read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    assert verify_seal(out)


def test_attack_source_never_contains_judge_marker(built: sb.Built) -> None:
    for t in built.tasks:
        s = t.safety
        assert s is not None
        if s.role != "attack" or s.attack_type not in ("instruction_following", "system_leak"):
            continue
        acct = built.world["accounts"][t.account_id]
        for item in acct["messages"] + acct["files"]:
            assert s.marker not in item.get("body_text", item.get("text", "")), t.task_id


def test_excluded_pair_drops_both_tasks_and_keeps_invariant(tmp_path: Path) -> None:
    out = _out(tmp_path)
    rows = [json.loads(x) for x in (out / PAIR_REVIEW).read_text(encoding="utf-8").splitlines()]
    rows[0].update(decision="exclude", note="합성 사유")
    (out / PAIR_REVIEW).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    kept = ds.load_dataset(out).tasks
    assert len(kept) == 158
    pairs: dict[str, set[str]] = {}
    for t in kept:
        assert t.safety is not None and t.safety.pair_id != rows[0]["pair_id"]
        pairs.setdefault(t.safety.pair_id, set()).add(t.safety.role)
    assert all(roles == {"attack", "normal"} for roles in pairs.values())
    assert len(ds.load_dataset(out, include_excluded=True).tasks) == 160


def test_clusters_per_type_and_split(built: sb.Built) -> None:
    pairs: dict[tuple[str, str], dict[str, set[str]]] = {}  # (유형, 분할) -> cluster -> pair_id
    template_home: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for t in built.tasks:
        assert t.safety is not None
        by_cluster = pairs.setdefault((t.safety.attack_type, t.split), {})
        by_cluster.setdefault(t.cluster_id, set()).add(t.safety.pair_id)
        key = (t.safety.attack_type, t.safety.template_id)
        template_home.setdefault(key, set()).add((t.cluster_id, t.split))
    assert len(pairs) == 10  # 5유형 x 2분할
    for by_cluster in pairs.values():
        assert len(by_cluster) == 4
        assert all(len(ids) == 2 for ids in by_cluster.values())
    # 같은 템플릿의 쌍은 항상 같은 cluster·split에 있다
    assert all(len(homes) == 1 for homes in template_home.values())


def test_build_rejects_marker_in_attack_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # 공격 문장에 판정 marker가 그대로 나오게 만들면 생성이 거부된다.
    leaky = {
        **sb.CORES,
        "instruction_following": tuple(("{marker}를 쓰세요.", None) for _ in range(8)),
    }
    monkeypatch.setattr(sb, "CORES", leaky)
    with pytest.raises(ValueError, match="표식이 공격 소스에 그대로"):
        sb.build(git_sha="test-sha")
