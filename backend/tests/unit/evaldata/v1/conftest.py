"""v1 구조 생성기 테스트 공용 픽스처. 작은 세계 모델은 같은 폴더의 small_world.toml이다."""

from pathlib import Path

import pytest

from orbit.evaldata.v1.build import build_skeleton, file_sha256
from orbit.evaldata.v1.skeleton import Skeleton
from orbit.evaldata.v1.worldmodel import WorldModel, load_world_model

HERE = Path(__file__).resolve().parent
SMALL_WORLD = HERE / "small_world.toml"
REAL_WORLD = HERE.parents[4] / "eval" / "worlds" / "orbit-v1.toml"
SEED = 20261116
SHA = "test-sha"


@pytest.fixture(scope="session")
def small_wm() -> WorldModel:
    return load_world_model(SMALL_WORLD)


@pytest.fixture(scope="session")
def small_sk(small_wm: WorldModel) -> Skeleton:
    return build_skeleton(small_wm, SEED, world_sha256=file_sha256(SMALL_WORLD), git_sha_=SHA)
