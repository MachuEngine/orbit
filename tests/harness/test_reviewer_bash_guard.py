"""code-reviewer Bash 가드 훅의 허용·차단 동작 테스트.

실행: python3 -m unittest discover -s tests/harness
"""

import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "reviewer_bash_guard.py"


def run_hook(command, raw=None):
    stdin = raw if raw is not None else json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )
    return subprocess.run(
        [sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True
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


if __name__ == "__main__":
    unittest.main()
