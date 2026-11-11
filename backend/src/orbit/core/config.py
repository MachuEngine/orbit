"""환경 변수 기반 설정. 접두는 `ORBIT_`이다.

비밀 값(DB URL의 비밀번호 포함)에는 기본값을 두지 않는다.
- `ORBIT_DATABASE_URL`: 앱 런타임용. `orbit_app` 역할(소유자 아님, NOBYPASSRLS)로 접속한다.
- `ORBIT_MIGRATION_DATABASE_URL`: 마이그레이션용. `orbit_owner` 역할로 접속한다.
  앱 프로세스에는 설정하지 않는다.
- `ORBIT_ENV`: `dev`일 때만 개발용 로그인(`POST /api/dev/login`)이 열린다(6단계 로그인 전까지).
  기본값은 `prod`이고, `prod`로는 API 앱이 시작되지 않는다(로그인 구현 전이라 인증 수단이 없다).
- `ORBIT_DEV_WORKSPACE_ID`: 개발용 로그인이 세션을 발급할 워크스페이스. 없으면 로그인은 404다.
- `ORBIT_SESSION_TTL_SECONDS`: 쿠키 세션 유효 시간. 기본 12시간.
- `ORBIT_SIM_SNAPSHOT_PATH`, `ORBIT_SIM_ACCOUNT_ID`: 개발용 Sim 커넥터가 읽을 합성 스냅샷과 계정.
- `ORBIT_USER_TIMEZONE`: 비서가 상대 날짜를 계산할 때 쓰는 IANA 시간대.
"""

import uuid
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORBIT_", extra="ignore")

    database_url: SecretStr
    migration_database_url: SecretStr | None = None
    env: Literal["dev", "prod"] = "prod"
    dev_workspace_id: uuid.UUID | None = None
    session_ttl_seconds: float = Field(default=43200.0, gt=0)
    llm_model_id: str | None = None  # 설정 지문용. 비어 있으면 지문에 "unset"으로 들어간다
    sim_snapshot_path: str | None = None
    sim_account_id: str = "acct-alice"
    user_timezone: str = "Asia/Seoul"
    approval_ttl_seconds: float = Field(default=900.0, gt=0)  # D5-5 후보값 15분
    sse_poll_interval_seconds: float = Field(default=0.25, gt=0)


def get_settings() -> Settings:
    """환경 변수에서 설정을 읽는다. 필수 값이 없으면 ValidationError가 난다."""
    return Settings()


class LLMSettings(BaseSettings):
    """LLM 호출 설정. DB·API 경로가 이 설정 없이 동작하도록 `Settings`와 분리했다.

    - `ORBIT_LLM_MODEL_ID`: 고정 모델 ID (D9-3). 형식 검증은 `orbit.llm.model_id`가 한다.
    - `ORBIT_LLM_PROVIDER_ORDER`: 쉼표로 구분한 제공자 우선순위 (D9-4).
    - `ORBIT_LLM_ACCEPTED_RESPONSE_MODEL_IDS`: 쉼표 구분. 요청 ID와 다르지만 같은 모델로
      인정할 응답 모델 ID 목록. 기본은 빈 값.
    - `ORBIT_OPENROUTER_API_KEY`: 기본값 없음, 빈 값 거절. provider가 openrouter일 때만 필수.
    - `ORBIT_LLM_PROVIDER`: `openrouter`(기본) | `openai`(ADR-0014, 파일럿용 임시).
      openai이면 `ORBIT_OPENAI_API_KEY`가 필수이고 `ORBIT_LLM_PROVIDER_ORDER`는 쓰지 않는다.
    - `ORBIT_OPENAI_BASE_URL`: 기본 `https://api.openai.com/v1`, https만 허용.
    - `ORBIT_LLM_REASONING_EFFORT`: openai 추론 강도(none|minimal|low|medium|high|xhigh).
      비우면 보내지 않는다.
    - `ORBIT_LLM_SEND_TEMPERATURE`: 기본 거짓. 거짓이면 openai 어댑터는 temperature를 보내지 않고
      그 사실을 설정 지문에 남긴다(추론 모델은 temperature를 지원하지 않을 수 있다).
    - `ORBIT_LLM_PRICE_INPUT_PER_MTOK`, `..._CACHED_INPUT_PER_MTOK`, `..._OUTPUT_PER_MTOK`,
      `ORBIT_LLM_PRICE_SOURCE`: 1백만 토큰당 달러 단가표(openai 비용 계산용). 하나라도 있으면
      네 값이 모두 있어야 한다. 없으면 비용은 None(미상)이다.
    """

    model_config = SettingsConfigDict(env_prefix="ORBIT_", extra="ignore")

    llm_model_id: str
    llm_provider: Literal["openrouter", "openai"] = "openrouter"
    llm_provider_order: Annotated[list[str], NoDecode] = Field(default_factory=list)
    openrouter_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    llm_reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    llm_send_temperature: bool = False
    llm_price_input_per_mtok: float | None = Field(default=None, ge=0)
    llm_price_cached_input_per_mtok: float | None = Field(default=None, ge=0)
    llm_price_output_per_mtok: float | None = Field(default=None, ge=0)
    llm_price_source: str | None = None
    llm_accepted_response_model_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)

    # API 키 빈 값 거절은 선택한 provider의 키에만 건다(`_provider_requirements`).
    # 쓰지 않는 provider의 키가 `.env`에 빈 값으로 남아 있어도 시작을 막지 않는다.

    @field_validator("llm_reasoning_effort", "llm_price_source", mode="before")
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        # `.env`에서 `KEY=`처럼 비워 둔 값은 설정하지 않은 것으로 본다.
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator(
        "llm_price_input_per_mtok",
        "llm_price_cached_input_per_mtok",
        "llm_price_output_per_mtok",
        mode="before",
    )
    @classmethod
    def _blank_price_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("openrouter_base_url", "openai_base_url")
    @classmethod
    def _require_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("base URL은 https://만 허용한다")
        return value

    @model_validator(mode="after")
    def _provider_requirements(self) -> "LLMSettings":
        key = self.openrouter_api_key if self.llm_provider == "openrouter" else self.openai_api_key
        if key is not None and not key.get_secret_value().strip():
            raise ValueError("API 키가 비어 있다")
        if self.llm_provider == "openrouter":
            if self.openrouter_api_key is None:
                raise ValueError("provider가 openrouter이면 ORBIT_OPENROUTER_API_KEY가 필요하다")
            if not self.llm_provider_order:
                raise ValueError("provider가 openrouter이면 ORBIT_LLM_PROVIDER_ORDER가 필요하다")
        elif self.openai_api_key is None:
            raise ValueError("provider가 openai이면 ORBIT_OPENAI_API_KEY가 필요하다")
        prices = (
            self.llm_price_input_per_mtok,
            self.llm_price_cached_input_per_mtok,
            self.llm_price_output_per_mtok,
        )
        if any(p is not None for p in prices) and (
            any(p is None for p in prices) or not self.llm_price_source
        ):
            raise ValueError("단가표는 입력·캐시 입력·출력 단가와 출처가 모두 있어야 한다")
        return self

    @field_validator("llm_accepted_response_model_ids", mode="before")
    @classmethod
    def _split_accepted_ids(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("llm_provider_order", mode="before")
    @classmethod
    def _split_provider_order(cls, value: object) -> object:
        if isinstance(value, str):
            value = [part.strip() for part in value.split(",") if part.strip()]
        return value


def get_llm_settings() -> LLMSettings:
    """환경 변수에서 LLM 설정을 읽는다. 필수 값이 없으면 ValidationError가 난다."""
    return LLMSettings()
