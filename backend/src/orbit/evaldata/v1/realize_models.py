"""본문 실현에 쓸 계열별 모델 설정과 비용 계산 (ADR-0026 §2.5·§2.6).

계열(openai, anthropic, google)마다 호출 경로·모델 ID·추론 강도·단가를 정한다. 기본값은 예시이며
실제 값은 실행 전에 공급자 가격표로 확인한다. TOML 설정 파일(`--models-config`)로 계열별로 덮어쓴다.

```toml
[openai]
route = "openrouter"          # "openai"(직접) | "openrouter"
model_id = "openai/gpt-6-luna"
reasoning_effort = "low"      # 직접 경로에서만 보낸다
temperature = 0.7            # 숫자 | "omit"(보내지 않는다)
max_output_tokens = 6000
price_input_per_mtok = 0.10
price_cached_input_per_mtok = 0.01
price_output_per_mtok = 0.50
price_source = "2026-11-16 공급자 가격표"
provider_order = ["OpenAI"]   # openrouter 경로에서 허용하는 제공자
```

`temperature = "omit"`은 temperature를 요청에 넣지 않는다. OpenRouter는 `require_parameters`로
라우팅하므로, 엔드포인트가 temperature를 지원하지 않는 모델(예: `anthropic/claude-haiku-5.5`의
Anthropic 엔드포인트)에 보내면 404가 난다. 이런 계열의 기본값은 보내지 않음이다.

비용은 호출마다 max(응답에 보고된 토큰 수 × 이 설정의 단가, 응답이 보고한 비용(OpenRouter
usage.cost))로 누적하고, 계산값과 보고값을 둘 다 기록한다.
캐시된 입력 토큰은 입력 토큰의 부분집합이고, 출력 토큰은 추론 토큰을 포함한다.
"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal, cast

from orbit.core.config import LLMSettings
from orbit.evaldata.v1.skeleton import Family
from orbit.llm.factory import build_llm
from orbit.llm.interface import LLM
from orbit.llm.openrouter import OpenRouterLLM
from orbit.llm.types import LLMUsage

FAMILIES: tuple[str, ...] = ("openai", "anthropic", "google")
Route = Literal["openai", "openrouter"]
LLMFactory = Callable[["FamilyModel"], LLM]

_DEFAULT_SOURCE = "기본값(예시). 실행 전에 공급자 가격표로 확인해야 한다"
_FIELDS = frozenset(
    {
        "route",
        "model_id",
        "reasoning_effort",
        "temperature",
        "max_output_tokens",
        "price_input_per_mtok",
        "price_cached_input_per_mtok",
        "price_output_per_mtok",
        "price_source",
        "provider_order",
        "timeout_seconds",
    }
)


class ModelConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FamilyModel:
    family: str
    route: Route
    model_id: str
    reasoning_effort: str | None
    temperature: float | None
    max_output_tokens: int
    price_input_per_mtok: float
    price_cached_input_per_mtok: float
    price_output_per_mtok: float
    price_source: str
    provider_order: tuple[str, ...] = ()
    timeout_seconds: float = 120.0

    def cost_usd(self, usage: LLMUsage) -> float:
        cached = min(usage.cached_input_tokens, usage.input_tokens)
        fresh = usage.input_tokens - cached
        total = fresh * self.price_input_per_mtok + cached * self.price_cached_input_per_mtok
        total += usage.output_tokens * self.price_output_per_mtok
        return total / 1_000_000

    def manifest(self) -> dict[str, Any]:
        d = asdict(self)
        d["provider_order"] = list(self.provider_order)
        # 어댑터가 실제로 보내는 값. 직접 경로는 temperature를 보내지 않고, OpenRouter 경로는
        # 추론 강도를 보내지 않는다.
        d["temperature_sent"] = self.route == "openrouter" and self.temperature is not None
        d["reasoning_effort_sent"] = self.route == "openai" and self.reasoning_effort is not None
        return d


def default_models() -> dict[str, FamilyModel]:
    return {
        "openai": FamilyModel(
            family="openai",
            route="openai",
            model_id="openai/gpt-6-luna",
            reasoning_effort="low",
            temperature=0.7,
            max_output_tokens=6000,
            price_input_per_mtok=0.10,
            price_cached_input_per_mtok=0.01,
            price_output_per_mtok=0.50,
            price_source=_DEFAULT_SOURCE,
            provider_order=("OpenAI",),
        ),
        "anthropic": FamilyModel(
            family="anthropic",
            route="openrouter",
            model_id="anthropic/claude-haiku-5.5",
            reasoning_effort=None,
            temperature=None,  # 이 모델의 Anthropic 엔드포인트는 temperature를 지원하지 않는다
            max_output_tokens=6000,
            price_input_per_mtok=0.10,
            price_cached_input_per_mtok=0.01,
            price_output_per_mtok=0.50,
            price_source=_DEFAULT_SOURCE,
            provider_order=("Anthropic",),
        ),
        "google": FamilyModel(
            family="google",
            route="openrouter",
            model_id="google/gemini-2.5-flash-lite",
            reasoning_effort=None,
            temperature=0.7,
            max_output_tokens=6000,
            price_input_per_mtok=0.10,
            price_cached_input_per_mtok=0.01,
            price_output_per_mtok=0.40,
            price_source=_DEFAULT_SOURCE,
            provider_order=("Google AI Studio",),
        ),
    }


def _apply(base: FamilyModel, values: Mapping[str, Any]) -> FamilyModel:
    unknown = set(values) - _FIELDS
    if unknown:
        raise ModelConfigError(f"{base.family}: 알 수 없는 설정 항목 {sorted(unknown)}")
    changes = dict(values)
    if "temperature" in changes:
        t = changes["temperature"]
        if t == "omit":
            changes["temperature"] = None
        elif isinstance(t, bool) or not isinstance(t, int | float):
            raise ModelConfigError(f'{base.family}: temperature는 숫자 또는 "omit"이어야 한다')
    if "provider_order" in changes:
        changes["provider_order"] = tuple(changes["provider_order"])
    route = changes.get("route", base.route)
    if route not in ("openai", "openrouter"):
        raise ModelConfigError(f"{base.family}: route는 openai 또는 openrouter여야 한다")
    for key in ("price_input_per_mtok", "price_cached_input_per_mtok", "price_output_per_mtok"):
        if key in changes and (not isinstance(changes[key], int | float) or changes[key] < 0):
            raise ModelConfigError(f"{base.family}: {key}는 0 이상의 수여야 한다")
    return replace(base, **changes)


def load_models(
    config_path: str | Path | None = None,
    *,
    openai_route: str | None = None,
) -> dict[str, FamilyModel]:
    """기본 설정에 TOML 파일과 `openai_route` 덮어쓰기를 차례로 적용한다."""
    models = default_models()
    if config_path is not None:
        with Path(config_path).open("rb") as f:
            data = tomllib.load(f)
        unknown = set(data) - set(FAMILIES)
        if unknown:
            raise ModelConfigError(f"알 수 없는 계열: {sorted(unknown)}")
        for family, values in data.items():
            if not isinstance(values, dict):
                raise ModelConfigError(f"{family}: 표(table)여야 한다")
            models[family] = _apply(models[family], values)
    if openai_route is not None:
        models["openai"] = _apply(models["openai"], {"route": openai_route})
    for m in models.values():
        # 단가 0은 비용을 0으로 계산해 상한을 무력화한다. 설정 실수로 보고 거절한다.
        if m.price_input_per_mtok <= 0 or m.price_output_per_mtok <= 0:
            raise ModelConfigError(f"{m.family}: 입력·출력 단가는 0보다 커야 한다")
    return models


def build_family_llm(model: FamilyModel) -> LLM:
    """실제 LLM 구현을 만든다.

    API 키는 `ORBIT_OPENAI_API_KEY`·`ORBIT_OPENROUTER_API_KEY`에서 읽는다.

    환경 변수에 있는 다른 LLM 설정(모델 ID, 단가 등)이 섞이지 않도록 모든 값을 명시한다.
    """
    settings = LLMSettings(
        llm_model_id=model.model_id,
        llm_provider=model.route,
        llm_provider_order=list(model.provider_order),
        llm_reasoning_effort=cast(Any, model.reasoning_effort),
        llm_send_temperature=False,
        llm_price_input_per_mtok=model.price_input_per_mtok,
        llm_price_cached_input_per_mtok=model.price_cached_input_per_mtok,
        llm_price_output_per_mtok=model.price_output_per_mtok,
        llm_price_source=model.price_source,
        llm_accepted_response_model_ids=[],
        llm_timeout_seconds=model.timeout_seconds,
        llm_max_retries=2,
    )
    if model.route == "openrouter":
        # 4xx 원인(error.message)을 오류 메시지에 남긴다. 이 호출은 합성 데이터만 보낸다.
        # temperature는 요청 값(None이면 생략)으로 제어하므로 어댑터는 막지 않는다.
        return OpenRouterLLM(settings, include_error_message=True, send_temperature=True)
    return build_llm(settings)


def family_of(value: str) -> Family:
    if value not in FAMILIES:
        raise ModelConfigError(f"알 수 없는 계열: {value}")
    return cast(Family, value)
