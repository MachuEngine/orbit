"""실제 세계 모델(eval/worlds/orbit-v1.toml)로 한 번 생성한 결과 검사."""

import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.evaldata.v1.build import generate
from orbit.evaldata.v1.skeleton import Skeleton, TzEventTruth
from orbit.evaldata.v1.worldmodel import WorldModel, load_world_model

from .conftest import REAL_WORLD, SEED, SHA

GENERATION_BUDGET_SECONDS = 30.0


@pytest.fixture(scope="module")
def real(tmp_path_factory: pytest.TempPathFactory) -> tuple[Skeleton, Path, float]:
    out = tmp_path_factory.mktemp("v1-real")
    t0 = time.monotonic()
    sk = generate(REAL_WORLD, SEED, out, SHA, template_world=True)
    return sk, out, time.monotonic() - t0


@pytest.fixture(scope="module")
def real_wm() -> WorldModel:
    return load_world_model(REAL_WORLD)


def test_generation_time_is_within_budget(real: tuple[Skeleton, Path, float]) -> None:
    assert real[2] < GENERATION_BUDGET_SECONDS


def test_scale_matches_targets(real: tuple[Skeleton, Path, float], real_wm: WorldModel) -> None:
    sk = real[0]
    t = real_wm.targets
    for u in real_wm.e2e_users():
        a = sk.accounts[u.account_id]
        assert abs(len(a.messages) - t.mails_per_e2e_user) <= t.mails_tolerance
        assert t.docs_per_e2e_user[0] <= len(a.files) <= t.docs_per_e2e_user[1]
        assert 2 <= len(a.calendars) <= 3
    for u, size in zip(real_wm.search_users(), t.search_sizes, strict=True):
        assert len(sk.accounts[u.account_id].messages) == size
    assert dict(Counter(s.type for s in sk.scenarios)) == t.scenarios


def test_stats_file(real: tuple[Skeleton, Path, float]) -> None:
    sk, out, _ = real
    stats = json.loads((out / "skeleton-stats.json").read_text("utf-8"))
    assert stats["unique_body_specs"]["total"] == len(sk.bodies)
    assert set(stats["generator_family"]) == {"openai", "anthropic", "google"}
    assert stats["target_chars_midpoint_sum"] > 0
    assert stats["all_day_events_total"] == sum(
        1 for a in sk.accounts.values() for e in a.events if e.all_day
    )
    ratios = stats["length_class"]["mail"]["ratios"]
    assert abs(ratios["short"] - 0.4) <= 0.03 and abs(ratios["long"] - 0.2) <= 0.03


def test_dst_change_is_inside_the_los_angeles_calendar(
    real: tuple[Skeleton, Path, float], real_wm: WorldModel
) -> None:
    sk = real[0]
    la = next(u for u in real_wm.e2e_users() if u.time_zone == "America/Los_Angeles")
    offsets = {
        e.start.astimezone(ZoneInfo(la.time_zone)).utcoffset()
        for e in sk.accounts[la.account_id].events
    }
    assert len(offsets) == 2


def test_template_world_loads_into_sim(real: tuple[Skeleton, Path, float]) -> None:
    sk, out, _ = real
    world = SimWorld.load(out / "world.template.json", FixedClock(sk.snapshot_time))
    big = world.account("acct-s5000")
    assert len(big.messages) == 5000


def test_tz_event_offsets_cover_dst_change(real: tuple[Skeleton, Path, float]) -> None:
    offsets = {
        s.truth.user_utc_offset_hours
        for s in real[0].scenarios
        if isinstance(s.truth, TzEventTruth) and s.truth.variant == "local"
    }
    assert offsets == {-7.0, -8.0}


def test_question_keys_are_unique_per_user(real: tuple[Skeleton, Path, float]) -> None:
    keys = [
        (s.account_id, s.truth.question_key)
        for s in real[0].scenarios
        if hasattr(s.truth, "question_key")
    ]
    assert len(keys) > 150 and len(keys) == len(set(keys))


def test_output_is_independent_of_hash_seed(tmp_path: Path) -> None:
    """PYTHONHASHSEED를 다르게 한 하위 프로세스 둘이 같은 skeleton.json을 만든다."""
    outs = []
    for i, seed in enumerate(("1", "2")):
        out = tmp_path / f"o{i}"
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "orbit.evaldata.v1.build",
                "--world",
                str(REAL_WORLD),
                "--seed",
                str(SEED),
                "--out",
                str(out),
                "--git-sha",
                SHA,
            ],
            check=True,
            capture_output=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        outs.append((out / "skeleton.json").read_bytes())
    assert outs[0] == outs[1]
