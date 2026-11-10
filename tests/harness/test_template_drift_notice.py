"""템플릿 소유 파일 수정 알림 훅 테스트.

실행: python3 -m unittest discover -s tests/harness
"""

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK = ROOT / ".claude" / "hooks" / "template_drift_notice.py"


def run_hook(file_path, session_id=None, raw=None):
    payload = {
        "session_id": session_id or str(uuid.uuid4()),
        "cwd": str(ROOT),
        "hook_event_name": "PostToolUse",
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path},
    }
    stdin = raw if raw is not None else json.dumps(payload)
    env = {"CLAUDE_PROJECT_DIR": str(ROOT), "PATH": "/usr/bin:/bin", "TMPDIR": tempfile.gettempdir()}
    return subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True, env=env)


class Notice(unittest.TestCase):
    def test_owned_files_trigger_notice(self):
        for path in [
            ".claude/agents/implementer.md",
            ".claude/hooks/reviewer_bash_guard.py",
            ".claude/harness/WORKFLOW.md",
            "scripts/harness-check.sh",
            "tests/harness/test_reviewer_bash_guard.py",
            ".github/pull_request_template.md",
            str(ROOT / ".claude" / "agents" / "code-reviewer.md"),  # 절대 경로
        ]:
            with self.subTest(path=path):
                result = run_hook(path)
                self.assertEqual(result.returncode, 0)
                out = json.loads(result.stdout)
                self.assertIn("템플릿 소유 파일", out["systemMessage"])
                self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
                self.assertIn("copier update", out["hookSpecificOutput"]["additionalContext"])

    def test_project_files_are_silent(self):
        for path in [
            "CLAUDE.md",
            "scripts/check.sh",
            ".claude/settings.json",
            ".gitignore",
            "backend/app.py",
            "docs/architecture.md",
            "/tmp/outside.txt",
            "../other-project/.claude/agents/x.md",
        ]:
            with self.subTest(path=path):
                result = run_hook(path)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), "")

    def test_same_file_notified_once_per_session(self):
        session = str(uuid.uuid4())
        first = run_hook(".claude/agents/implementer.md", session_id=session)
        second = run_hook(".claude/agents/implementer.md", session_id=session)
        other = run_hook(".claude/agents/code-reviewer.md", session_id=session)
        self.assertNotEqual(first.stdout.strip(), "")
        self.assertEqual(second.stdout.strip(), "")
        self.assertNotEqual(other.stdout.strip(), "")

    def test_malformed_input_never_blocks(self):
        for raw in ["", "not json", json.dumps({"tool_input": {}})]:
            with self.subTest(raw=raw):
                result = run_hook(None, raw=raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
