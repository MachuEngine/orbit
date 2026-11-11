"""code-reviewer Bash 가드 훅의 허용·차단 동작 테스트.

실행: python3 -m unittest discover -s tests/harness
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "reviewer_bash_guard.py"


def run_hook(command, raw=None, project_dir=None, cwd=None):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    if project_dir is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project_dir)
    stdin = raw if raw is not None else json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    return subprocess.run(
        [sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True, env=env, cwd=cwd
    )


class AllowedCommands(unittest.TestCase):
    def test_allowed(self):
        for command in [
            "git diff",
            "git diff --stat",
            "git diff HEAD~1 -- backend/app.py",
            "git log --oneline -n 20",
            "git show HEAD",
            "git status --short",
            "git ls-files",
            "git blame CLAUDE.md",
            "git grep -n user_id",
            "scripts/check.sh",
            "./scripts/check.sh",
            "bash scripts/check.sh",
            "ls",
            "ls -la docs",
            "ls -R backend",
            "ls .env.example",
            "grep -n user_id backend/app.py",
            "grep -in -A3 approval backend/app.py backend/run.py",
            "grep -c TODO .env.example",
            "git grep --untracked -n user_id",
        ]:
            with self.subTest(command=command):
                result = run_hook(command)
                self.assertEqual(result.returncode, 0, result.stderr)


class BlockedCommands(unittest.TestCase):
    def test_blocked(self):
        for command in [
            "rm -rf backend",
            "git commit -m x",
            "git checkout -- .",
            "git reset --hard",
            "git push",
            "git diff --output=patch.txt",
            "git log --output patch.txt",
            "git diff --ext-diff",
            "git grep -O foo",
            "git grep --open-files-in-pager=vim foo",
            "git diff; rm -rf .",
            "git diff && touch x",
            "git diff | tee out.txt",
            "git diff > out.txt",
            "git diff $(touch x)",
            "git diff `touch x`",
            "FOO=1 git diff",
            "cd /tmp",
            "python3 -c 'print(1)'",
            "bash scripts/check.sh --fix",
            "sed -i s/a/b/ CLAUDE.md",
            "git diff\ntouch x",
            "",
            # ls·grep 비밀정보 경로
            "ls .env",
            "ls -la backend/.env.local",
            "ls secrets",
            "ls secrets/",
            "grep KEY .env",
            "grep KEY backend/.env",
            "grep KEY .env.production",
            "grep KEY secrets/dev-data-key",
            "grep BEGIN certs/server.pem",
            "grep -f .env app.py",
            "grep --file=.env app.py",
            "grep -f.env app.py",
            "grep -fsecrets/dev-data-key app.py",
            # grep 재귀 검색
            "grep -r KEY .",
            "grep -R KEY backend",
            "grep -rn KEY backend",
            "grep -nr KEY backend",
            "grep --recursive KEY .",
            "grep --dereference-recursive KEY .",
            "grep -d recurse KEY .",
            "grep --directories=recurse KEY .",
            # ls·grep 이외의 조회 명령은 여전히 차단
            "find . -name x -delete",
            "cat CLAUDE.md",
        ]:
            with self.subTest(command=command):
                result = run_hook(command)
                self.assertEqual(result.returncode, 2, f"차단되지 않음: {command!r}")
                self.assertIn("차단", result.stderr)

    def test_malformed_input_is_blocked(self):
        for raw in ["", "not json", json.dumps({"tool_input": {}})]:
            with self.subTest(raw=raw):
                self.assertEqual(run_hook(None, raw=raw).returncode, 2)


class ProjectDirCommands(unittest.TestCase):
    """CLAUDE_PROJECT_DIR를 쓰는 경로 규칙과 테스트·정적 검사 허용 규칙."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = pathlib.Path(self._tmp.name).resolve()
        self.project = base / "proj"
        (self.project / "backend").mkdir(parents=True)
        (self.project / "scripts").mkdir()
        self.outside = base / "outside"
        self.outside.mkdir()
        # 프로젝트 안에서 밖을 가리키는 심볼릭 링크
        (self.project / "escape").symlink_to(self.outside)
        # 자체 .git을 가진 중첩 저장소(폴더 형태와 파일 형태)
        (self.project / "nested" / ".git").mkdir(parents=True)
        (self.project / "nested_file").mkdir()
        (self.project / "nested_file" / ".git").write_text("gitdir: ../x\n")
        self.p = str(self.project)
        self.o = str(self.outside)

    def assert_allowed(self, command):
        result = run_hook(command, project_dir=self.project, cwd=self.project)
        self.assertEqual(result.returncode, 0, f"{command!r}: {result.stderr}")

    def assert_blocked(self, command, project_dir="default"):
        project = self.project if project_dir == "default" else project_dir
        result = run_hook(command, project_dir=project, cwd=self.project)
        self.assertEqual(result.returncode, 2, f"차단되지 않음: {command!r}")
        self.assertIn("차단", result.stderr)

    def test_allowed(self):
        p = self.p
        for command in [
            f"{p}/scripts/check.sh",
            f"bash {p}/scripts/check.sh",
            f"{p}/backend/../scripts/check.sh",
            "scripts/check.sh",
            f"git -C {p} status",
            f"git -C {p}/backend diff --stat",
            f"git -C {p} log --oneline -n 5",
            f"git -C {p} check-ignore -v .env",
            "git check-ignore .env",
            "uv run pytest",
            "uv run pytest -q tests/test_a.py -k approval",
            f"uv run --directory {p}/backend pytest -q",
            f"uv run --project {p}/backend pytest",
            f"uv run --directory={p}/backend pytest",
            "pytest -x tests",
            "python -m pytest -q",
            "python3 -m pytest tests",
            "uv run ruff check --no-fix",
            "uv run ruff check --no-fix backend --select E",
            "uv run ruff format --check",
            "uv run ruff format --check --diff backend",
            "uv run mypy backend",
            "uv run pyright",
            f"uv run --directory {p}/backend ruff check --no-fix .",
            f"uv run --directory {p}/backend ruff format --check .",
            f"uv run --project {p}/backend mypy app",
            f"uv run --directory {p}/backend pyright",
            # 허용 목록 방식 추가 사례
            f"uv run --directory {p}/backend pytest -q -x tests/unit",
            'pytest -k "name"',
            'pytest -m "not slow" -qx --tb=short --maxfail=3 --durations=5 -ra',
            "pytest --collect-only --co --lf --ff --no-header -vv -s -l",
            "pytest tests/test_a.py::test_one",
            "uv run ruff check --no-fix --statistics backend",
            "uv run ruff check --quiet --diff --output-format=json --select=E,F backend",
            "uv run ruff check --no-fix --output-format full",
            "uv run ruff format --check backend",
            "uv run ruff format --diff --quiet backend",
            "uv run mypy --strict backend",
            "uv run mypy --pretty --show-error-codes --no-error-summary --show-column-numbers backend",
            "uv run pyright --outputjson --stats backend",
            "python -m pytest -m slow",
            f"git -C {p} diff HEAD~1 -- backend",
            # git 허용 목록 방식
            "git status --short",
            "git status -s -b --porcelain --untracked-files=no",
            "git diff --stat HEAD~1",
            "git diff -U5 --cached --name-status a..b",
            "git diff --word-diff HEAD...main",
            "git log --oneline -n 5",
            "git log -5 --stat --graph --decorate -p --follow -- backend/app.py",
            "git show HEAD --stat",
            "git show HEAD:backend/app.py",
            "git blame -L 1,5 CLAUDE.md",
            "git blame -L 1,+5 CLAUDE.md",
            "git grep -n -F outline -- src",
            "git grep --untracked -n x",
            "git grep -n -i -w -l -c -E -e pattern --cached",
            "git ls-files --others --exclude-standard",
            "git ls-files --cached -m",
            "git check-ignore -v .env",
            "uv run ruff check --no-fix backend",
            f"git -C {p}/backend status --short",
        ]:
            with self.subTest(command=command):
                self.assert_allowed(command)

    def test_blocked(self):
        p, o = self.p, self.o
        for command in [
            # check.sh: 프로젝트 밖, 인자 추가, 심볼릭 링크 우회
            f"{o}/scripts/check.sh",
            "/tmp/scripts/check.sh",
            f"bash {o}/scripts/check.sh",
            f"{p}/scripts/check.sh --fix",
            f"bash {p}/scripts/check.sh --fix",
            f"{p}/escape/scripts/check.sh",
            # git -C
            "git -C /tmp status",
            f"git -C {o} log",
            f"git -C {p}/escape log",
            f"git -C {p} commit -m x",
            f"git -C {p} push",
            f"git -C {p} diff --output=x.txt",
            f"git -C {p} grep -O foo",
            "git -C",
            # 프로젝트 밖 --directory/--project
            f"uv run --directory {o} pytest",
            f"uv run --project {o} pytest",
            f"uv run --directory={o} pytest",
            "uv run --directory /tmp pytest",
            f"uv run --directory {p}/escape pytest",
            "uv run --directory",
            # uv run 뒤의 다른 명령과 옵션
            "uv run python -c 'print(1)'",
            "uv run python script.py",
            "uv run bash -c ls",
            "uv run --with requests pytest",
            "uv run",
            "uv sync",
            "uv pip install x",
            # ruff
            "uv run ruff check --fix",
            "uv run ruff check --unsafe-fixes",
            "uv run ruff check --fix-only",
            "uv run ruff check --add-noqa",
            "uv run ruff format",
            "uv run ruff format backend",
            "uv run ruff",
            "uv run ruff clean",
            # 파일을 쓰는 옵션
            "uv run pytest -p myplugin",
            "uv run pytest --basetemp=/tmp/x",
            "uv run pytest --junitxml=out.xml",
            "uv run mypy --install-types",
            "uv run mypy --junit-xml out.xml app",
            # 셸 확장 문자
            "git -C ~ status",
            "git -C '~' status",
            "git -C ~/x status",
            f"uv run --directory ~/x pytest",
            "pytest {a,b}",
            "pytest tests/*.py",
            "pytest tests/test_?.py",
            "pytest tests/[ab].py",
            "pytest --directory=~/x",
            "git diff !x",
            # --directory와 --project 동시 사용
            f"uv run --directory {p}/backend --project {p}/backend pytest",
            f"uv run --project {p}/backend --directory {p}/backend pytest",
            "uv run --directory backend --project backend pytest",
            # ruff 옵션
            "uv run ruff check --config fix=true",
            "uv run ruff check --config=x.toml",
            "uv run ruff format -- --check x",
            "uv run ruff format --check -- x",
            "uv run ruff check --isolated",
            "uv run ruff check --cache-dir=x",
            "uv run ruff check --output-file=x",
            "uv run ruff check --watch",
            "uv run ruff check --output-format=bad",
            "uv run ruff check --select",
            "uv run ruff check --select=E --select=F",
            "uv run ruff check /tmp/outside",
            "uv run ruff format --check ../..",
            "uv run ruff format --check --config x",
            # pytest 옵션
            "uv run pytest --log-file=x",
            "uv run pytest --debug=x",
            "uv run pytest -qp plugin",
            "uv run pytest -o cache_dir=x",
            "uv run pytest -o addopts=x",
            "uv run pytest -c x.ini",
            "uv run pytest --rootdir=/tmp",
            "uv run pytest --confcutdir=/tmp",
            "uv run pytest --pdbcls=m:C",
            "uv run pytest --pyargs x",
            "uv run pytest --override-ini=addopts=x",
            "uv run pytest --cache-clear",
            "uv run pytest --cache-dir=x",
            "uv run pytest --tb=bad",
            "uv run pytest --maxfail=x",
            "uv run pytest -kname",
            "uv run pytest -k",
            "uv run pytest -k a -k b",
            "uv run pytest -qz",
            "uv run pytest --",
            "uv run pytest @args.txt",
            "uv run pytest -",
            "uv run pytest ::name",
            "uv run pytest /tmp/outside",
            "pytest ../..",
            f"pytest {o}",
            f"pytest {p}/escape",
            f"pytest {p}/escape/x.py::t",
            f"uv run --directory {p}/backend pytest ../../..",
            # python -m pytest
            "python -m pytest -p x",
            "python -I -m pytest",
            "python -m pytest -m x -m y",
            "python -m x -m pytest",
            "python -c pytest",
            "python3 -m pytest -c x.ini",
            # mypy, pyright
            "uv run mypy --python-executable x",
            "uv run mypy --cache-dir=x",
            "uv run mypy --config-file=x",
            "uv run mypy --junit-x x",
            "uv run mypy --shadow-file a b",
            "uv run mypy @args.txt",
            "uv run mypy --strict /tmp/outside",
            "uv run pyright --pythonpath x",
            "uv run pyright --project x",
            "uv run pyright --watch",
            "uv run pyright --createstub x",
            "uv run pyright /tmp/outside",
            # git 보강
            "git grep --no-index K -- .env",
            f"git -C {p} diff --no-index /dev/null .env",
            "git diff --no-index a b",
            "git show HEAD:.env",
            "git grep KEY -- .env",
            "git diff -- secrets/dev-data-key",
            "git show HEAD:certs/server.pem",
            f"git -C {p}/nested status",
            f"git -C {p}/nested_file status",
            f"git -C {p}/nested/ log",
            # git 허용 목록 밖 옵션
            "git grep --open=echo -n x -- f",
            "git grep --open x",
            "git grep --open-files-in-pager x",
            "git grep -O x",
            "git grep -f /etc/hosts",
            "git grep --untracked --no-exclude-standard -n KEY",
            "git grep --exclude-standard x",
            "git grep --no-index x",
            "git grep -nr x",
            "git grep",
            "git blame --contents /etc/hosts -L1,2 CLAUDE.md",
            "git blame --contents /etc/hosts -L 1,2 CLAUDE.md",
            "git blame -L :func CLAUDE.md",
            "git diff --ext-diff",
            "git diff --output=x",
            "git diff --stat=x",
            "git diff --textconv",
            "git log --format=%x",
            "git log --pretty=oneline",
            "git log --output=x",
            "git log -n",
            "git log -n x",
            "git show --format=x HEAD",
            "git show HEAD --ext-diff",
            "git status --untracked-files=bad",
            "git status --ignored",
            "git status -sb",
            "git ls-files --others",
            "git ls-files --others --cached",
            "git ls-files -o --exclude-standard",
            "git ls-files --exclude-from=x",
            "git ls-files -x a",
            "git check-ignore --stdin",
            "git check-ignore -v --no-index x",
            # git 위치 인자 형태
            "git diff /etc/hosts /etc/passwd",
            "git diff ../outside ../x",
            "git show HEAD:/etc/hosts",
            "git show HEAD:../x",
            "git show :0:file",
            "git show HEAD:",
            "git diff -- /etc/hosts",
            "git diff -- ../x",
            "git diff -",
            # 비밀정보 경로는 대소문자를 구분하지 않는다
            "grep KEY .ENV",
            "grep KEY backend/.Env.local",
            "ls SECRETS/",
            "ls Secrets",
            "grep BEGIN certs/server.PEM",
            "grep BEGIN a.KEY",
            "git show HEAD:.Env",
            "git grep -n KEY -- Secrets",
            "git diff -- SECRETS/x",
            # 중첩 저장소 하위 폴더, 폴더 아닌 상위에 .git이 있는 경우
            f"git -C {p}/nested/sub status",
            f"git -C {p}/nested_file/sub/deeper log",
            f"git -C {p}/nested/sub/ diff",
            # ruff check는 --no-fix 필수
            "uv run ruff check backend",
            "uv run ruff check",
            "uv run ruff check --statistics backend",
            f"uv run --directory {p}/backend ruff check .",
            "uv run ruff check --no-fix --fix",
            # 셸 메타문자는 계속 금지
            "uv run pytest && touch x",
            "uv run pytest | tee out.txt",
            "uv run pytest > out.txt",
            "uv run pytest $(touch x)",
            # 비밀정보 경로
            "uv run pytest .env",
            "pytest secrets/dev-data-key",
            # 도구 이름만 비슷한 명령
            "python -m pip install x",
            "python3 -m http.server",
            "python -c 'print(1)'",
            "ruff check --fix",
            "pytest; rm -rf .",
        ]:
            with self.subTest(command=command):
                self.assert_blocked(command)

    def test_git_ancestor_nested_repo(self):
        (self.project / "nested" / "sub").mkdir()
        (self.project / "nested_file" / "sub" / "deeper").mkdir(parents=True)
        (self.project / "nested" / "sub").mkdir(exist_ok=True)
        # 하위 폴더에서 인자 없이 실행해도 같은 검사를 한다
        for cwd in [
            self.project / "nested",
            self.project / "nested" / "sub",
            self.project / "nested_file" / "sub" / "deeper",
        ]:
            with self.subTest(cwd=str(cwd)):
                result = run_hook("git status", project_dir=self.project, cwd=cwd)
                self.assertEqual(result.returncode, 2, result.stderr)
        result = run_hook("git status", project_dir=self.project, cwd=self.project / "backend")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_git_cwd_outside_project_blocked(self):
        result = run_hook("git status", project_dir=self.project, cwd=self.outside)
        self.assertEqual(result.returncode, 2)
        self.assertIn("차단", result.stderr)

    def test_no_positional_tool_requires_project_cwd(self):
        for command in ["pytest", "python -m pytest -q", "uv run pytest", "uv run ruff check --no-fix",
                        "uv run mypy --strict", "uv run pyright", "uv run ruff format --check"]:
            with self.subTest(command=command):
                result = run_hook(command, project_dir=self.project, cwd=self.outside)
                self.assertEqual(result.returncode, 2, f"차단되지 않음: {command!r}")
                self.assertIn("차단", result.stderr)
        # --directory가 프로젝트 안쪽이면 cwd가 밖이어도 그 폴더 기준이다
        result = run_hook(f"uv run --directory {self.p}/backend pytest", project_dir=self.project, cwd=self.outside)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ruff_check_without_no_fix_message(self):
        result = run_hook("uv run ruff check backend", project_dir=self.project, cwd=self.project)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--no-fix", result.stderr)

    def test_project_dir_forms_blocked_without_env(self):
        p = self.p
        for command in [
            f"{p}/scripts/check.sh",
            f"bash {p}/scripts/check.sh",
            f"git -C {p} status",
            f"uv run --directory {p}/backend pytest",
            f"uv run --project {p}/backend pytest",
        ]:
            with self.subTest(command=command):
                self.assert_blocked(command, project_dir=None)

    def test_forms_without_dir_work_without_env(self):
        for command in ["scripts/check.sh", "git status", "uv run pytest", "python3 -m pytest"]:
            with self.subTest(command=command):
                result = run_hook(command)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()


class FailClosed(unittest.TestCase):
    """예상하지 못한 입력이나 내부 오류도 차단(종료 코드 2)으로 끝나는지 확인한다."""

    def test_nul_character_blocked(self):
        result = run_hook("git status\x00--output=x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("NUL", result.stderr)

    def test_internal_exception_is_blocked(self):
        # main() 안에서 예상하지 못한 예외가 나도 종료 코드 1(통과)이 아니라 2(차단)로 끝나야 한다.
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('guard', {str(HOOK)!r})\n"
            "guard = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(guard)\n"
            "def boom():\n"
            "    raise RuntimeError('unexpected')\n"
            "guard.main = boom\n"
            "guard.run()\n"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("훅 내부 오류", result.stderr)


class GitGlobalOptionsBlocked(unittest.TestCase):
    """서브커맨드 앞 전역 옵션은 허용 목록에 없으므로 차단된다(회귀 방지)."""

    def test_global_options_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            proj = os.path.realpath(tmp)
            os.makedirs(os.path.join(proj, "a"))
            for command in [
                "git -c core.pager=less status",
                "git --git-dir=x status",
                "git --work-tree=x status",
                "git -P log",
                f"git -C {proj} -C {proj}/a status",
            ]:
                with self.subTest(command=command):
                    result = run_hook(command, project_dir=proj, cwd=proj)
                    self.assertEqual(result.returncode, 2, f"차단되지 않음: {command!r}")
