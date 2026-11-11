"""환경 변수 기반 설정. 접두는 `ORBIT_`이다.

비밀 값(DB URL의 비밀번호 포함)에는 기본값을 두지 않는다.
- `ORBIT_DATABASE_URL`: 앱 런타임용. `orbit_app` 역할(소유자 아님, NOBYPASSRLS)로 접속한다.
- `ORBIT_MIGRATION_DATABASE_URL`: 마이그레이션용. `orbit_owner` 역할로 접속한다.
  앱 프로세스에는 설정하지 않는다.
"""

from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORBIT_", extra="ignore")

    database_url: SecretStr
    migration_database_url: SecretStr | None = None


def get_settings() -> Settings:
    """환경 변수에서 설정을 읽는다. 필수 값이 없으면 ValidationError가 난다."""
    return Settings()


class LLMSettings(BaseSettings):
    """LLM 호출 설정. DB·API 경로가 이 설정 없이 동작하도록 `Settings`와 분리했다.

    - `ORBIT_LLM_MODEL_ID`: 고정 모델 ID (D9-3). 형식 검증은 `orbit.llm.model_id`가 한다.
    - `ORBIT_LLM_PROVIDER_ORDER`: 쉼표로 구분한 제공자 우선순위 (D9-4).
    - `ORBIT_LLM_ACCEPTED_RESPONSE_MODEL_IDS`: 쉼표 구분. 요청 ID와 다르지만 같은 모델로
      인정할 응답 모델 ID 목록. 기본은 빈 값.
    - `ORBIT_OPENROUTER_API_KEY`: 기본값 없음, 빈 값 거절.
    """

    model_config = SettingsConfigDict(env_prefix="ORBIT_", extra="ignore")

    llm_model_id: str
    llm_provider_order: Annotated[list[str], NoDecode]
    openrouter_api_key: SecretStr = Field(min_length=1)
    llm_accepted_response_model_ids: Annotated[list[str], NoDecode] = Field(default_factory=list)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)

    @field_validator("openrouter_base_url")
    @classmethod
    def _require_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("OpenRouter base URL은 https://만 허용한다")
        return value

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
        if isinstance(value, list) and not value:
            raise ValueError("제공자 우선순위가 비어 있다")
        return value


def get_llm_settings() -> LLMSettings:
    """환경 변수에서 LLM 설정을 읽는다. 필수 값이 없으면 ValidationError가 난다."""
    return LLMSettings()
