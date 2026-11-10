#!/usr/bin/env python3
"""code-reviewer 서브에이전트의 Bash 사용을 읽기 전용 명령으로 제한하는 PreToolUse 훅.

허용: git의 조회 명령(diff, log, show, status, ls-files, blame, grep), scripts/check.sh, ls, grep.
차단: 그 밖의 모든 명령, 셸 메타문자, 파일을 쓰거나 외부 프로그램을 실행할 수 있는 git 옵션.

ls·grep 추가 제한:
- 비밀정보 경로(.env, secrets/, *.pem, *.key)를 가리키는 인자는 차단한다. .env.example은 허용한다.
- grep의 재귀 검색(-r, -R 등)은 차단한다. 재귀 검색은 .gitignore를 따르는 `git grep --untracked`를 쓴다.
  Bash 안의 재귀 grep은 권한 deny 규칙이 막지 못해 .env 내용이 출력될 수 있기 때문이다.

판단할 수 없으면 차단한다(fail closed). 종료 코드 2와 stderr 메시지로 차단 사유를 에이전트에 돌려준다.
"""

import json
import re
import shlex
import sys

ALLOWED_GIT_SUBCOMMANDS = {"diff", "log", "show", "status", "ls-files", "blame", "grep"}

# 파일 쓰기나 외부 프로그램 실행으로 이어질 수 있는 git 옵션
BLOCKED_GIT_OPTION_PREFIXES = (
    "--output",               # git diff/log --output=<file> 은 파일을 쓴다
    "--ext-diff",             # 외부 diff 프로그램 실행
    "--open-files-in-pager",  # git grep: 페이저 프로그램 실행
    "-O",                     # git grep -O 의 짧은 형태
)

CHECK_SCRIPT_FORMS = (
    ["scripts/check.sh"],
    ["./scripts/check.sh"],
    ["bash", "scripts/check.sh"],
)

SHELL_METACHARACTERS = set(";&|<>`$()\n\\")

# .env, .env.local, backend/.env, secrets/..., *.pem, *.key (단, .env.example 제외)
# 앞쪽 대안 ^-[A-Za-z]+ 는 -f.env 처럼 옵션에 붙여 쓴 경로를 잡는다.
SENSITIVE_PATH = re.compile(
    r"(^|[/=]|^-[A-Za-z]+)\.env(?!\.example$)(\.[^/]*)?$"
    r"|(^|[/=]|^-[A-Za-z]+)secrets(/|$)"
    r"|\.(pem|key)$"
)

GREP_RECURSIVE_LONG = ("--recursive", "--dereference-recursive", "--directories=recurse", "--directories")


def check_sensitive_args(tokens):
    for token in tokens[1:]:
        if SENSITIVE_PATH.search(token):
            block(f"비밀정보 경로를 가리키는 인자 {token}")


def is_recursive_grep_option(token):
    if token.startswith(GREP_RECURSIVE_LONG) or token == "-d":
        return True
    # -rn, -Rin 처럼 묶인 짧은 옵션
    return token.startswith("-") and not token.startswith("--") and any(c in token[1:] for c in "rRd")


def block(reason: str) -> None:
    print(f"[reviewer-bash-guard] 차단: {reason}. code-reviewer는 읽기 전용 명령만 쓸 수 있다.", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        command = payload["tool_input"]["command"]
    except Exception:
        block("훅 입력을 해석할 수 없다")
        return

    if not isinstance(command, str) or not command.strip():
        block("빈 명령")

    if any(ch in SHELL_METACHARACTERS for ch in command):
        block("셸 메타문자(; & | < > ` $ ( ) \\ 줄바꿈)는 쓸 수 없다")

    try:
        tokens = shlex.split(command)
    except ValueError:
        block("명령을 해석할 수 없다")
        return

    if tokens in [list(form) for form in CHECK_SCRIPT_FORMS]:
        sys.exit(0)

    if len(tokens) >= 2 and tokens[0] == "git" and tokens[1] in ALLOWED_GIT_SUBCOMMANDS:
        for token in tokens[2:]:
            if token.startswith(BLOCKED_GIT_OPTION_PREFIXES):
                block(f"허용하지 않는 git 옵션 {token}")
        sys.exit(0)

    if tokens[0] == "ls":
        check_sensitive_args(tokens)
        sys.exit(0)

    if tokens[0] == "grep":
        for token in tokens[1:]:
            if is_recursive_grep_option(token):
                block(f"grep 재귀 검색 옵션 {token}. 재귀 검색은 git grep --untracked를 쓴다")
        check_sensitive_args(tokens)
        sys.exit(0)

    block(f"허용 목록에 없는 명령: {tokens[0]}")


if __name__ == "__main__":
    main()
