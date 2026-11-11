"""환경 변수 기반 설정. 접두는 `ORBIT_`이다.

비밀 값(DB URL의 비밀번호 포함)에는 기본값을 두지 않는다.
- `ORBIT_DATABASE_URL`: 앱 런타임용. `orbit_app` 역할(소유자 아님, NOBYPASSRLS)로 접속한다.
- `ORBIT_MIGRATION_DATABASE_URL`: 마이그레이션용. `orbit_owner` 역할로 접속한다.
  앱 프로세스에는 설정하지 않는다.
"""

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORBIT_", extra="ignore")

    database_url: SecretStr
    migration_database_url: SecretStr | None = None


def get_settings() -> Settings:
    """환경 변수에서 설정을 읽는다. 필수 값이 없으면 ValidationError가 난다."""
    return Settings()
