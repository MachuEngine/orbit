#!/usr/bin/env python3
"""code-reviewer 서브에이전트의 Bash 사용을 읽기 전용 명령으로 제한하는 PreToolUse 훅.

허용: git의 조회 명령(diff, log, show, status, ls-files, blame, grep, check-ignore), scripts/check.sh, ls, grep,
      테스트·정적 검사(pytest, ruff check, ruff format --check, mypy, pyright).
차단: 그 밖의 모든 명령, 셸 메타문자, 허용 목록에 없는 git 옵션.

경로 규칙 (환경 변수 CLAUDE_PROJECT_DIR 기준, os.path.realpath로 정규화해 비교):
- check.sh는 상대 경로 형태 외에 `<CLAUDE_PROJECT_DIR>/scripts/check.sh`(앞에 `bash` 허용)와 정확히 일치할 때만 허용한다.
- `git -C <dir>`, `uv run --directory <dir>`, `uv run --project <dir>`의 <dir>은 프로젝트 폴더 안쪽만 허용한다.
  CLAUDE_PROJECT_DIR가 없으면 이 형태는 모두 차단한다.

테스트·정적 검사 제한 (허용 목록 방식, 목록에 없는 옵션은 모두 차단):
- `uv run` 뒤에는 pytest, ruff, mypy, pyright만 올 수 있다. uv 옵션은 `--directory`와 `--project` 중 하나만 허용한다.
- 각 도구는 TOOL_SPECS에 적은 옵션만 쓸 수 있다. 위치 인자는 경로여야 하고 CLAUDE_PROJECT_DIR 안쪽이어야 한다
  (`--directory`가 있으면 그 폴더 기준으로 해석한다). pytest는 `경로::테스트이름`도 허용한다.
- `python -m pytest`는 정확히 이 형태만 허용한다(`python -I`, `python -c` 등은 차단).
- ruff format은 `--check` 또는 `--diff`가 있어야 한다. `--`는 차단한다.
- ruff check는 `--no-fix` 또는 `--diff`가 있어야 한다(설정 파일의 `fix = true`가 파일을 고치지 못하게 한다).
- 위치 인자가 없으면 기준 폴더(훅 작업 폴더 또는 `--directory`)가 프로젝트 안쪽이어야 한다. CLAUDE_PROJECT_DIR가 없으면 검사하지 않는다.

셸 확장: 명령 문자열에 `* ? [ ] { } !`가 있거나 토큰이 `~`로 시작하면 차단한다(따옴표 안도 차단).
git (허용 목록 방식, GIT_SPECS에 적은 옵션만 쓸 수 있다. 옵션 이름은 정확히 일치해야 하며 약어는 차단한다):
- 위치 인자는 `[A-Za-z0-9_./~^-]+` 형태의 커밋·범위·경로여야 한다. 절대 경로와 `..` 경로 조각은 차단한다.
  `--` 뒤는 경로로 본다. `git show`는 `<커밋>:<경로>`도 허용한다. `git grep`의 첫 위치 인자(또는 `-e` 값)는 검색 패턴이다.
- 비밀정보 경로 인자는 차단한다(check-ignore는 내용을 읽지 않으므로 제외). 대소문자는 구분하지 않는다.
- `git ls-files --others`는 `--exclude-standard`와 함께 쓸 때만 허용한다.
- `git -C <dir>`의 <dir>부터 프로젝트 루트 직전까지의 모든 상위 폴더에 `.git`(폴더·파일)이 있으면 차단한다(중첩 저장소).
  `-C`가 없으면 훅의 작업 폴더로 같은 검사를 한다. CLAUDE_PROJECT_DIR가 없으면 이 검사는 하지 않는다.

알려진 위험: pytest는 저장소의 테스트 코드와 conftest.py, pytest.ini 설정을 실행·적용한다. 테스트 코드가 파일을 쓰거나 네트워크에
접근해도 이 훅은 막지 못한다. `uv run`은 `.venv`를 만들거나 동기화하고 로컬 빌드 코드를 실행하며 네트워크로 패키지를 받을 수 있다.
완전한 읽기 전용이 아니며 이를 받아들이고 허용한 것이다(docs/hir/HIR-0003 참고).

ls·grep 추가 제한:
- 비밀정보 경로(.env, secrets/, *.pem, *.key)를 가리키는 인자는 차단한다. .env.example은 허용한다.
- grep의 재귀 검색(-r, -R 등)은 차단한다. 재귀 검색은 .gitignore를 따르는 `git grep --untracked`를 쓴다.
  Bash 안의 재귀 grep은 권한 deny 규칙이 막지 못해 .env 내용이 출력될 수 있기 때문이다.
  비밀정보 경로 검사는 대소문자를 구분하지 않는다(.ENV, Secrets/, a.PEM).

판단할 수 없으면 차단한다(fail closed). 종료 코드 2와 stderr 메시지로 차단 사유를 에이전트에 돌려준다.
"""

import json
import os
import re
import shlex
import sys

# git 서브커맨드별 허용 옵션. flags: 값 없는 옵션(이름 정확히 일치), patterns: 한 토큰 형태 옵션(fullmatch),
# values: 다음 토큰을 값으로 받는 옵션(값은 fullmatch).
GIT_SPECS = {
    "status": {
        "flags": {"--short", "-s", "--porcelain", "-b", "--branch"},
        "patterns": [r"--untracked-files=(no|normal|all)"],
        "values": {},
    },
    "diff": {
        "flags": {"--stat", "--name-only", "--name-status", "--cached", "--staged", "--word-diff"},
        "patterns": [r"-U[0-9]+"],
        "values": {},
    },
    "log": {
        "flags": {"--oneline", "--stat", "--name-only", "--graph", "--decorate", "-p", "--follow"},
        "patterns": [r"-[0-9]+"],
        "values": {"-n": r"[0-9]+"},
    },
    "show": {
        "flags": {"--stat", "--name-only"},
        "patterns": [],
        "values": {},
    },
    "ls-files": {
        "flags": {"--others", "--exclude-standard", "--cached", "-m"},
        "patterns": [],
        "values": {},
    },
    "blame": {
        "flags": set(),
        "patterns": [],
        "values": {"-L": r"[0-9]+(,[0-9]+|,\+[0-9]+)?"},
    },
    "grep": {
        "flags": {"-n", "-i", "-w", "-l", "-c", "-F", "-E", "--untracked", "--cached"},
        "patterns": [],
        "values": {"-e": r".+"},
    },
    "check-ignore": {
        "flags": {"-v"},
        "patterns": [],
        "values": {},
    },
}

# 커밋·범위(a..b, a...b)·경로 위치 인자. 맨 앞 `-`는 옵션이므로 허용하지 않는다.
GIT_REV_OR_PATH = re.compile(r"[A-Za-z0-9_./~^][A-Za-z0-9_./~^-]*")

CHECK_SCRIPT_FORMS = (
    ["scripts/check.sh"],
    ["./scripts/check.sh"],
    ["bash", "scripts/check.sh"],
)

CHECK_SCRIPT_RELATIVE_FORMS = [list(form) for form in CHECK_SCRIPT_FORMS]

SHELL_METACHARACTERS = set(";&|<>`$()\n\\*?[]{}!")
TILDE_TOKEN = re.compile(r"(^|[=:])~")

# .env, .env.local, backend/.env, secrets/..., *.pem, *.key (단, .env.example 제외)
# 앞쪽 대안 ^-[A-Za-z]+ 는 -f.env 처럼 옵션에 붙여 쓴 경로를 잡는다.
SENSITIVE_PATH = re.compile(
    r"(^|[/=:]|^-[A-Za-z]+)\.env(?!\.example$)(\.[^/]*)?$"
    r"|(^|[/=:]|^-[A-Za-z]+)secrets(/|$)"
    r"|\.(pem|key)$",
    re.IGNORECASE,
)

GREP_RECURSIVE_LONG = ("--recursive", "--dereference-recursive", "--directories=recurse", "--directories")


# 도구별 허용 옵션. flags: 값 없는 옵션, values: 값을 받는 옵션(정규식), bundle: 묶어 쓸 수 있는 짧은 옵션 글자,
# once: 한 번만 쓸 수 있는 옵션, path_split: 위치 인자에서 경로 뒤 `::`를 떼어 낸다.
TOOL_SPECS = {
    "pytest": {
        "flags": {"-ra", "-rA", "--no-header", "--collect-only", "--co", "--lf", "--ff"},
        "values": {
            "--tb": r"(short|long|line|no|native)",
            "--maxfail": r"[0-9]+",
            "--durations": r"[0-9]+",
            "-k": r"[^-].*",
            "-m": r"[^-].*",
        },
        "space_only": {"-k", "-m"},
        "once": {"-k", "-m"},
        "bundle": "qvxsl",
        "path_split": True,
    },
    "ruff check": {
        "flags": {"--statistics", "--quiet", "--diff", "--no-fix"},
        "values": {
            "--output-format": r"(concise|full|json|github)",
            "--select": r"[A-Za-z0-9,]+",
        },
        "space_only": set(),
        "once": {"--output-format", "--select"},
        "bundle": "",
        "path_split": False,
    },
    "ruff format": {
        "flags": {"--check", "--diff", "--quiet"},
        "values": {},
        "space_only": set(),
        "once": set(),
        "bundle": "",
        "path_split": False,
    },
    "mypy": {
        "flags": {"--strict", "--pretty", "--show-error-codes", "--no-error-summary", "--show-column-numbers"},
        "values": {},
        "space_only": set(),
        "once": set(),
        "bundle": "",
        "path_split": False,
    },
    "pyright": {
        "flags": {"--outputjson", "--stats"},
        "values": {},
        "space_only": set(),
        "once": set(),
        "bundle": "",
        "path_split": False,
    },
}
UV_DIR_OPTIONS = ("--directory", "--project")


def project_dir():
    raw = os.environ.get("CLAUDE_PROJECT_DIR")
    return os.path.realpath(raw) if raw else None


def is_inside_project(path: str) -> bool:
    base = project_dir()
    if base is None:
        return False
    real = os.path.realpath(path)
    return real == base or real.startswith(base + os.sep)


def is_project_check_script(path: str) -> bool:
    base = project_dir()
    return base is not None and os.path.realpath(path) == os.path.join(base, "scripts", "check.sh")


def is_check_script_command(tokens) -> bool:
    if tokens in CHECK_SCRIPT_RELATIVE_FORMS:
        return True
    if len(tokens) == 1 and os.path.isabs(tokens[0]):
        return is_project_check_script(tokens[0])
    if len(tokens) == 2 and tokens[0] == "bash" and os.path.isabs(tokens[1]):
        return is_project_check_script(tokens[1])
    return False


def check_path_arg(arg, base, split):
    """위치 인자는 CLAUDE_PROJECT_DIR 안쪽의 경로여야 한다. base는 상대 경로를 해석할 기준 폴더."""
    path = arg.split("::", 1)[0] if split else arg
    if not path or path.startswith(("-", "@")):
        block(f"경로가 아닌 인자 {arg}")
    if not is_inside_project(os.path.join(base, path)):
        block(f"프로젝트 폴더 밖 경로 {arg}")


def check_with_spec(name, args, base):
    spec = TOOL_SPECS[name]
    used_once = set()
    has_positional = False
    i = 0
    while i < len(args):
        token = args[i]
        i += 1
        if not token.startswith("-") or token == "-":
            if token == "-":
                block(f"{name}에서 허용하지 않는 인자 -")
            check_path_arg(token, base, spec["path_split"])
            has_positional = True
            continue
        if token in spec["flags"]:
            continue
        if spec["bundle"] and re.fullmatch(r"-[" + spec["bundle"] + r"]+", token):
            continue
        opt, eq, val = token.partition("=")
        if opt in spec["values"]:
            if eq:
                if opt in spec["space_only"]:
                    block(f"{name}에서 허용하지 않는 옵션 형태 {token}")
            else:
                if i >= len(args):
                    block(f"{name} {opt}에 값이 없다")
                val, i = args[i], i + 1
            if not re.fullmatch(spec["values"][opt], val):
                block(f"{name} {opt}의 값이 허용 범위 밖이다: {val}")
            if opt in spec["once"]:
                if opt in used_once:
                    block(f"{name} {opt}를 두 번 쓸 수 없다")
                used_once.add(opt)
            continue
        block(f"{name}에서 허용하지 않는 옵션 {token}")
    # 위치 인자가 없으면 도구가 기준 폴더 전체를 대상으로 하므로 기준 폴더가 프로젝트 안쪽이어야 한다
    if not has_positional and project_dir() is not None and not is_inside_project(base):
        block(f"위치 인자가 없는 {name}의 기준 폴더가 프로젝트 폴더 밖이다 {base}")


def check_tool(tool, args, base):
    """tool(pytest, ruff, mypy, pyright)과 그 인자를 검사한다. 허용되지 않으면 block()이 종료한다."""
    check_sensitive_args([None] + args)
    if tool == "ruff":
        if not args or args[0] not in ("check", "format"):
            block("ruff는 check와 format --check만 쓸 수 있다")
        name = "ruff " + args[0]
        rest = args[1:]
        if name == "ruff format" and "--check" not in rest and "--diff" not in rest:
            block("ruff format은 파일을 고치므로 --check 또는 --diff 없이는 쓸 수 없다")
        if name == "ruff check" and "--no-fix" not in rest and "--diff" not in rest:
            block("ruff check는 설정 파일의 fix = true로 파일을 고칠 수 있으므로 --no-fix를 붙여야 한다")
        check_with_spec(name, rest, base)
    else:
        check_with_spec(tool, args, base)


def parse_uv_run(tokens):
    """`uv run [--directory|--project <dir>] <tool> args...`을 (tool, args, base)로 나눈다. 아니면 None."""
    if len(tokens) < 3 or tokens[0] != "uv" or tokens[1] != "run":
        return None
    i = 2
    directory_used = None
    base = os.getcwd()
    while i < len(tokens) and tokens[i].startswith("-"):
        token = tokens[i]
        if token in UV_DIR_OPTIONS:
            if i + 1 >= len(tokens):
                block(f"{token}에 경로가 없다")
            option, directory, i = token, tokens[i + 1], i + 2
        elif token.startswith(tuple(opt + "=" for opt in UV_DIR_OPTIONS)):
            option, directory, i = token.split("=", 1)[0], token.split("=", 1)[1], i + 1
        else:
            block(f"uv run에서 허용하지 않는 옵션 {token}")
            return None
        if directory_used is not None:
            block("--directory와 --project는 하나만, 한 번만 쓸 수 있다")
        directory_used = option
        if not is_inside_project(directory):
            block(f"프로젝트 폴더 밖 경로 {directory}")
        if option == "--directory":
            base = os.path.realpath(directory)
    if i >= len(tokens):
        return None
    if tokens[i] not in ("pytest", "ruff", "mypy", "pyright"):
        block(f"uv run으로 실행할 수 없는 명령: {tokens[i]}")
    return tokens[i], tokens[i + 1:], base


def check_sensitive_args(tokens):
    for token in tokens[1:]:
        if SENSITIVE_PATH.search(token):
            block(f"비밀정보 경로를 가리키는 인자 {token}")


def is_recursive_grep_option(token):
    if token.startswith(GREP_RECURSIVE_LONG) or token == "-d":
        return True
    # -rn, -Rin 처럼 묶인 짧은 옵션
    return token.startswith("-") and not token.startswith("--") and any(c in token[1:] for c in "rRd")


def check_not_nested_repo(directory):
    """directory부터 프로젝트 루트 직전까지의 모든 폴더에 .git(폴더·파일)이 있으면 차단한다."""
    base = project_dir()
    if base is None:
        return
    real = os.path.realpath(directory)
    while real != base and real != os.path.dirname(real):
        if os.path.lexists(os.path.join(real, ".git")):
            block(f"중첩 저장소 {real}에서는 git을 쓸 수 없다")
        real = os.path.dirname(real)


def check_git_path(path):
    if path.startswith("/") or ".." in path.split("/"):
        block(f"git 경로 인자는 절대 경로나 .. 조각을 쓸 수 없다: {path}")


def check_git_positional(token, allow_colon):
    if allow_colon and ":" in token:
        rev, path = token.split(":", 1)
        if not GIT_REV_OR_PATH.fullmatch(rev) or not path or ":" in path:
            block(f"허용하지 않는 git 인자 {token}")
        check_git_path(path)
        return
    if not GIT_REV_OR_PATH.fullmatch(token):
        block(f"허용하지 않는 git 인자 {token}")
    check_git_path(token)


def check_git_args(sub, args):
    spec = GIT_SPECS[sub]
    if sub != "check-ignore":
        check_sensitive_args([None] + args)
    seen = set()
    positionals = []
    has_pattern_option = False
    after_dashdash = False
    i = 0
    while i < len(args):
        token = args[i]
        i += 1
        if after_dashdash:
            check_git_path(token)
        elif token == "--":
            after_dashdash = True
        elif token.startswith("-"):
            if token in spec["flags"]:
                seen.add(token)
            elif any(re.fullmatch(p, token) for p in spec["patterns"]):
                pass
            elif token in spec["values"]:
                if i >= len(args):
                    block(f"git {sub} {token}에 값이 없다")
                value, i = args[i], i + 1
                if not re.fullmatch(spec["values"][token], value):
                    block(f"git {sub} {token}의 값이 허용 범위 밖이다: {value}")
                if token == "-e":
                    has_pattern_option = True
            else:
                block(f"git {sub}에서 허용하지 않는 옵션 {token}")
        else:
            positionals.append(token)
    if sub == "grep" and not has_pattern_option:
        if not positionals:
            block("git grep에 검색 패턴이 없다")
        positionals = positionals[1:]
    for token in positionals:
        check_git_positional(token, allow_colon=(sub == "show"))
    if sub == "ls-files" and "--others" in seen and "--exclude-standard" not in seen:
        block("git ls-files --others는 --exclude-standard와 함께 써야 한다")


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

    if "\x00" in command:
        block("NUL 문자는 쓸 수 없다")

    if any(ch in SHELL_METACHARACTERS for ch in command):
        block("셸 메타문자·확장 문자(; & | < > ` $ ( ) \\ * ? [ ] { } ! 줄바꿈)는 쓸 수 없다")

    try:
        tokens = shlex.split(command)
    except ValueError:
        block("명령을 해석할 수 없다")
        return

    if any(TILDE_TOKEN.search(t) for t in tokens):
        block("틸데(~) 확장은 쓸 수 없다")

    if is_check_script_command(tokens):
        sys.exit(0)

    if tokens[0] == "git":
        rest = tokens[1:]
        if len(rest) >= 2 and rest[0] == "-C":
            if not is_inside_project(rest[1]):
                block(f"프로젝트 폴더 밖 경로 {rest[1]}")
            check_not_nested_repo(rest[1])
            rest = rest[2:]
        elif project_dir() is not None:
            if not is_inside_project(os.getcwd()):
                block("훅의 작업 폴더가 프로젝트 폴더 밖이다")
            check_not_nested_repo(os.getcwd())
        if rest and rest[0] in GIT_SPECS:
            check_git_args(rest[0], rest[1:])
            sys.exit(0)

    parsed = parse_uv_run(tokens)
    if parsed is None and tokens[0] == "pytest":
        parsed = ("pytest", tokens[1:], os.getcwd())
    if parsed is None and tokens[0] in ("python", "python3") and tokens[1:3] == ["-m", "pytest"]:
        parsed = ("pytest", tokens[3:], os.getcwd())
    if parsed is not None:
        check_tool(*parsed)
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


def run() -> None:
    """예상하지 못한 예외도 차단으로 끝낸다(fail closed).

    Claude Code는 종료 코드 2만 차단으로 처리한다. 처리되지 않은 예외는 종료 코드 1이 되어
    명령이 그대로 실행되므로, sys.exit(SystemExit)만 통과시키고 나머지 예외는 block으로 바꾼다.
    """
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 어떤 예외든 차단으로 끝내는 것이 목적이다
        block(f"훅 내부 오류({type(exc).__name__})")


if __name__ == "__main__":
    run()
