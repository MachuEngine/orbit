import subprocess
from pathlib import Path

import pytest

from orbit.evalrunner.cli import _git_state


def _g(repo: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],  # noqa: S607
        cwd=repo,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _g(tmp_path, "init", "-q")
    (tmp_path / "backend" / "src").mkdir(parents=True)
    (tmp_path / "backend" / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    _g(tmp_path, "add", ".")
    _g(tmp_path, "commit", "-q", "-m", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_clean_tree(repo: Path) -> None:
    sha, dirty = _git_state()
    assert len(sha) == 40 and dirty is False


def test_modified_tracked_file_is_dirty(repo: Path) -> None:
    (repo / "backend" / "src" / "a.py").write_text("x = 2\n", encoding="utf-8")
    assert _git_state()[1] is True


def test_untracked_code_is_dirty_but_results_folder_is_not(repo: Path) -> None:
    (repo / "eval" / "results").mkdir(parents=True)
    (repo / "eval" / "results" / "r.jsonl").write_text("{}\n", encoding="utf-8")
    assert _git_state(repo / "eval" / "results")[1] is False
    assert _git_state()[1] is True  # 출력 폴더를 모르면 eval/ 아래 새 파일은 변경이다
    (repo / "backend" / "src" / "b.py").write_text("y = 1\n", encoding="utf-8")
    assert _git_state(repo / "eval" / "results")[1] is True


def test_untracked_files_elsewhere_are_ignored(repo: Path) -> None:
    (repo / "notes.txt").write_text("메모\n", encoding="utf-8")
    assert _git_state()[1] is False
