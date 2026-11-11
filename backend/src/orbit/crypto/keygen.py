"""개발용 KEK 파일 생성 도우미: `python -m orbit.crypto.keygen <path>`.

무작위 32바이트를 base64로 파일에 쓰고 권한을 0600으로 둔다. 이미 있는 파일은 덮어쓰지 않는다.
키 값은 출력하지 않는다. 운영 KEK는 KMS를 쓴다(ADR-0016 §3, 9단계).
"""

import sys

from orbit.crypto.envelope import write_new_kek_file


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("사용법: python -m orbit.crypto.keygen <키 파일 경로>", file=sys.stderr)
        return 2
    try:
        write_new_kek_file(argv[1])
    except FileExistsError:
        print(f"이미 있는 파일이라 덮어쓰지 않는다: {argv[1]}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"키 파일을 만들 수 없다: {argv[1]} ({type(exc).__name__})", file=sys.stderr)
        return 1
    print(f"KEK 파일을 만들었다(권한 0600): {argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
