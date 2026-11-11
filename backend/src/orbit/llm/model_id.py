"""고정 모델 ID 검증 (D9-3).

실험 재현성을 위해 모델 ID는 가리키는 대상이 시간이 지나도 바뀌지 않는 값이어야 한다.
거절 규칙(하나라도 해당하면 `InvalidModelIdError`):

1. 빈 값이거나 공백(앞뒤 포함, 중간 포함)이 있다.
2. `provider/model` 형식이 아니다. OpenRouter는 이 형식을 쓰고, 제공자 접두가 없으면 어떤
   모델로 해석될지 알 수 없다. 두 부분 모두 비어 있으면 안 된다.
3. 별칭이다. 모델 부분이 `latest`이거나 `latest`로 끝난다(`-latest`, `:latest` 등).
   대소문자는 구분하지 않는다.
4. OpenRouter 자동 라우터·가변 변형이다. 제공자 접두가 `openrouter`(예: `openrouter/auto`)이거나
   변형 접미가 `:floating`·`:nitro`·`:online`이다. 실제 응답 모델이 요청마다 바뀌거나 동작이
   달라진다. `:free`·`:thinking`·`:exacto`는 고정 모델의 별도 엔드포인트라 허용한다.
5. 모델 부분(변형 접미 제외)에 숫자가 하나도 없다. 날짜(`2024-08-06`)나 버전(`4.5`, `70b`)
   같은 식별자가 전혀 없으면 별칭일 가능성이 높다고 본다.

한계: 규칙 5는 숫자가 있다는 사실만 본다. 날짜가 없어도 버전 숫자가 있는 ID(예:
`google/gemini-2.5-flash`)는 통과하는데, 제공자가 이 ID 뒤의 가중치를 바꿀 수 있다. 그 경우는
실제 응답 모델 ID 대조(`ModelMismatchError`)와 파일럿 ADR에서 모델 ID를 확정하며 보완한다.
"""

import re

_ALIAS_SUFFIX = re.compile(r"(^|[-_.:])latest$", re.IGNORECASE)
_FORBIDDEN_VARIANTS = frozenset({"floating", "nitro", "online"})
_FORBIDDEN_PROVIDERS = frozenset({"openrouter"})


_DATE_SUFFIX = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})")


def is_declared_model(declared: str, actual: str) -> bool:
    """실제 응답 모델이 선언 모델과 같은 모델인가. 선언 ID에 날짜 접미가 붙은 것까지 같다고 본다.

    어댑터의 허용 규칙(`-YYYY-MM-DD`, `-YYYYMMDD`)과 같다. 허용 목록(`accepted`)은 반영하지 않는다.
    """
    if actual == declared:
        return True
    return (
        actual.startswith(declared) and _DATE_SUFFIX.fullmatch(actual[len(declared) :]) is not None
    )


class InvalidModelIdError(ValueError):
    """고정 모델 ID 규칙을 어긴 값."""


def validate_model_id(model_id: str) -> str:
    """규칙을 통과하면 입력을 그대로 돌려주고, 아니면 `InvalidModelIdError`를 낸다."""
    if not model_id or not model_id.strip():
        raise InvalidModelIdError("모델 ID가 비어 있다")
    if model_id != model_id.strip() or any(ch.isspace() for ch in model_id):
        raise InvalidModelIdError("모델 ID에 공백이 있다")
    provider, sep, model = model_id.partition("/")
    if not sep or not provider or not model or "/" in model:
        raise InvalidModelIdError("모델 ID는 `provider/model` 형식이어야 한다")
    if provider.lower() in _FORBIDDEN_PROVIDERS:
        raise InvalidModelIdError("자동 라우터는 고정 모델 ID가 아니다")
    base, _, variant = model.partition(":")
    if not base:
        raise InvalidModelIdError("모델 이름이 비어 있다")
    if variant.lower() in _FORBIDDEN_VARIANTS:
        raise InvalidModelIdError(f"`:{variant}` 변형은 응답 모델이 고정되지 않는다")
    if _ALIAS_SUFFIX.search(model) or _ALIAS_SUFFIX.search(base):
        raise InvalidModelIdError("`latest` 별칭은 고정 모델 ID가 아니다")
    if not any(ch.isdigit() for ch in base):
        raise InvalidModelIdError("날짜나 버전 식별자가 없는 모델 ID는 고정 ID로 보지 않는다")
    return model_id
