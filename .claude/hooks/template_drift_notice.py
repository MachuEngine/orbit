#!/usr/bin/env python3
"""템플릿 소유 파일을 수정하면 템플릿 저장소에도 반영하라고 알리는 PostToolUse 훅.

Edit·Write 도구로 `.claude/harness/owned-files.txt`에 있는 경로를 고치면
- 사용자에게: systemMessage로 알림을 띄운다.
- Claude에게: additionalContext로 템플릿 반영을 제안하라고 전달한다.

같은 세션에서 같은 파일은 한 번만 알린다. 알림 훅이므로 어떤 경우에도 작업을 막지 않는다(항상 종료 코드 0).
"""

import hashlib
import json
import os
import sys
import tempfile

OWNED_LIST = os.path.join(".claude", "harness", "owned-files.txt")


def load_owned(project_dir):
    path = os.path.join(project_dir, OWNED_LIST)
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                entries.append(line)
    return entries


def is_owned(rel_path, entries):
    for entry in entries:
        if entry.endswith("/"):
            if rel_path.startswith(entry):
                return True
        elif rel_path == entry:
            return True
    return False


def already_notified(session_id, rel_path):
    """세션별 상태 파일에 기록해 같은 파일을 반복해서 알리지 않는다."""
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    state = os.path.join(tempfile.gettempdir(), f"claude-harness-notice-{key}.txt")
    seen = set()
    if os.path.exists(state):
        with open(state, encoding="utf-8") as f:
            seen = {line.rstrip("\n") for line in f}
    if rel_path in seen:
        return True
    with open(state, "a", encoding="utf-8") as f:
        f.write(rel_path + "\n")
    return False


def main():
    try:
        payload = json.load(sys.stdin)
        file_path = payload["tool_input"]["file_path"]
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or payload.get("cwd") or os.getcwd()
        project_dir = os.path.realpath(project_dir)
        abs_path = os.path.realpath(os.path.join(project_dir, file_path))
        rel_path = os.path.relpath(abs_path, project_dir).replace(os.sep, "/")
        if rel_path.startswith("../"):
            return
        if not is_owned(rel_path, load_owned(project_dir)):
            return
        if already_notified(str(payload.get("session_id", "")), rel_path):
            return
    except Exception:
        return  # 알림 훅은 실패해도 작업을 막지 않는다

    user_message = (
        f"[하네스] 템플릿 소유 파일을 수정했습니다: {rel_path}\n"
        "다른 프로젝트에도 필요한 변경이면 하네스 템플릿 저장소에도 반영하세요. "
        "절차는 .claude/harness/WORKFLOW.md의 '하네스 개선 절차'에 있습니다."
    )
    claude_context = (
        f"템플릿 소유 파일 {rel_path}을(를) 수정했다. 이 파일은 하네스 템플릿 저장소"
        "(.copier-answers.yml의 _src_path)가 관리한다. 변경이 다른 프로젝트에도 해당하면, "
        "현재 작업을 마치기 전에 개발자에게 템플릿 저장소 반영을 제안한다: "
        "템플릿 수정 → 템플릿 테스트 → HIR·CHANGELOG 갱신 → 버전 태그·push → 각 프로젝트에서 copier update. "
        "이 프로젝트에만 해당하는 변경이면 템플릿 소유 파일 대신 CLAUDE.md나 scripts/check.sh에서 해결하도록 제안한다."
    )
    print(json.dumps({
        "systemMessage": user_message,
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": claude_context,
        },
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
    sys.exit(0)
