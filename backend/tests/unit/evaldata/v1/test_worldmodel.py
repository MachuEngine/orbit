"""세계 모델 v1 로드·검증."""

import tomllib
from typing import Any

import pytest

from orbit.evaldata.v1.worldmodel import WorldModelError, load_world_model, parse_world_model

from .conftest import REAL_WORLD, SMALL_WORLD


def _data(path: object = SMALL_WORLD) -> dict[str, Any]:
    with open(str(path), "rb") as fh:
        return tomllib.load(fh)


def test_real_world_model_matches_spec() -> None:
    wm = load_world_model(REAL_WORLD)
    e2e, search = wm.e2e_users(), wm.search_users()
    assert len(e2e) == 6 and len(search) == 3
    assert sorted(u.time_zone for u in e2e).count("Asia/Seoul") == 5
    assert [u.time_zone for u in e2e].count("America/Los_Angeles") == 1
    assert 55 <= len(wm.people) <= 65
    assert len(wm.homonym_pairs()) >= 2
    assert all(2 <= len(u.calendars) <= 3 for u in e2e)
    assert wm.targets.search_sizes == [200, 1000, 5000]
    assert all(2 <= sum(1 for p in wm.projects if p.user == u.key) <= 3 for u in e2e)
    assert wm.window.snapshot.weekday() < 5


def test_small_world_loads() -> None:
    wm = load_world_model(SMALL_WORLD)
    assert len(wm.e2e_users()) == 2 and len(wm.search_users()) == 3


def test_rejects_unknown_scenario_type() -> None:
    d = _data()
    d["targets"]["scenarios"]["no_such_type"] = 1
    with pytest.raises(WorldModelError, match="알 수 없는 시나리오"):
        parse_world_model(d)


def test_rejects_variant_sum_mismatch() -> None:
    d = _data()
    d["targets"]["variants"]["absent.no_amount"] = 5
    with pytest.raises(WorldModelError, match="갈래 수 합"):
        parse_world_model(d)


def test_rejects_real_domain() -> None:
    d = _data()
    d["people"][0]["email"] = "someone@gmail.com"
    with pytest.raises(WorldModelError, match="도메인"):
        parse_world_model(d)


def test_requires_two_homonym_pairs() -> None:
    d = _data()
    d["people"][1]["name"] = "다른이름"
    with pytest.raises(WorldModelError, match="동명이인"):
        parse_world_model(d)


def test_search_users_must_share_persona() -> None:
    d = _data()
    d["users"][-1]["name"] = "다른 사람"
    with pytest.raises(WorldModelError, match="같은 인물 설정"):
        parse_world_model(d)


def test_rejects_extra_fields() -> None:
    d = _data()
    d["meta"]["extra"] = 1
    with pytest.raises(WorldModelError):
        parse_world_model(d)
