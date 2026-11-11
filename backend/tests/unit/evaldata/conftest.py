from pathlib import Path

import pytest

from orbit.evaldata.generate import generate

REPO_ROOT = Path(__file__).resolve().parents[4]
WORLD_MODEL = REPO_ROOT / "eval" / "worlds" / "orbit-v0.toml"
COMMITTED_DATASET = REPO_ROOT / "eval" / "datasets" / "orbit-eval" / "v0"
SEED = 20261111
SHA = "test-sha"


@pytest.fixture(scope="session")
def dataset_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("evaldata") / "ds"
    generate(WORLD_MODEL, SEED, out, SHA)
    return out
