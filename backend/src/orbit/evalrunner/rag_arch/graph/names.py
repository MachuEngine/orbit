"""개체 이름 정규화(ADR-0036 A4). 같은 이름 개체를 합치는 규칙이다. 바꾸면 색인 해시가 바뀐다.

1. NFKC, 소문자, 공백·문장부호 제거(`squash`). 글자·숫자만 남는다.
2. person이면 끝의 호칭(님, 씨, 팀장, 대표 ...)을 하나 떼어 낸다. 떼고 남는 글자가 2자 미만이면
   떼지 않는다.
질문 문장은 `squash`만 한다(호칭을 떼면 일반 단어가 잘린다).
"""

import re
import unicodedata

_PUNCT = re.compile(r"[\W_]+")
_TITLES = tuple(
    sorted(
        [
            "님",
            "씨",
            "대표",
            "대표님",
            "팀장",
            "팀장님",
            "부장",
            "부장님",
            "과장",
            "과장님",
            "차장",
            "차장님",
            "대리",
            "대리님",
            "사원",
            "이사",
            "이사님",
            "상무",
            "전무",
            "사장",
            "사장님",
            "매니저",
            "매니저님",
            "교수",
            "교수님",
            "선생",
            "선생님",
        ],
        key=len,
        reverse=True,
    )
)


def squash(text: str) -> str:
    return _PUNCT.sub("", unicodedata.normalize("NFKC", text).casefold())


def strip_title(key: str) -> str:
    for title in _TITLES:
        if key.endswith(title) and len(key) - len(title) >= 2:
            return key[: -len(title)]
    return key


def name_key(name: str, kind: str) -> str:
    key = squash(name)
    return strip_title(key) if kind == "person" else key
