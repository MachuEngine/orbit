"""데이터 봉투 (ADR-0018 §2).

도구 결과와 대화 맥락은 모델에 넘기기 직전에 이 봉투로 감싼다. 봉투는 표시 계층이라 저장소에는
순수 JSON을 두고, LLM 메시지를 만들 때만 씌운다.

- 안쪽 텍스트의 `<`·`>`는 `&lt;`·`&gt;`로 바꾼다. 데이터가 봉투를 닫거나 태그를 흉내 낼 수 없다.
- `boundary`는 요청 경로마다 다른 12자 16진수다. 실사용은 run ID에서, 평가는 에피소드 ID에서
  결정적으로 만든다(같은 입력이면 같은 프롬프트).
"""

import hashlib
import re

BOUNDARY_LENGTH = 12
_BOUNDARY = re.compile(rf"[0-9a-f]{{{BOUNDARY_LENGTH}}}")
_SOURCE = re.compile(r"[A-Za-z0-9_]+")
_SOURCE_MAX = 40


def boundary_for(seed: str) -> str:
    """`seed`에서 결정적으로 만든 12자 16진수 경계값."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:BOUNDARY_LENGTH]


def safe_source(name: str) -> str:
    """도구 이름을 봉투 `source`로 쓸 수 있게 영숫자·밑줄만 남긴다. 모델이 낸 이름도 받는다."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name)[:_SOURCE_MAX]
    return cleaned or "unknown"


def wrap_data(json_text: str, *, source: str, boundary: str) -> str:
    """JSON 텍스트를 데이터 봉투로 감싼다. `source`는 영숫자·밑줄, `boundary`는 12자 16진수."""
    if not _SOURCE.fullmatch(source):
        raise ValueError("source는 영숫자와 밑줄만 쓸 수 있다")
    if not _BOUNDARY.fullmatch(boundary):
        raise ValueError(f"boundary는 {BOUNDARY_LENGTH}자 16진수여야 한다")
    inner = json_text.replace("<", "&lt;").replace(">", "&gt;")
    return (
        f'<orbit_data source="{source}" boundary="{boundary}">\n'
        f"{inner}\n"
        f'</orbit_data boundary="{boundary}">'
    )
