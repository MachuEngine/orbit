"""시작 시 안전 점검 (ADR-0022 §3·§5). 하나라도 어긋나면 시작을 거부한다.

메시지에는 값(주소, 역할 이름, 리비전 번호)이 아니라 무엇이 문제인지만 쓴다.

- 환경 점검(`ensure_deploy_environment`, 동기): `RENDER`가 있는데 `ORBIT_ENV=dev`, prod인데
  로그인 허용 목록이 비어 있음, prod에서 내장 워커를 `google` 커넥터 없이 켬.
- DB 점검(`ensure_database_ready`, prod에서만): 접속 역할이 RLS 격리 조건에 맞는지, DB 리비전이
  코드의 최신 리비전과 같은지.
"""

import os
from collections.abc import Mapping
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from orbit.core.config import Settings
from orbit.db.session import AsyncDatabase, RoleFlags

__all__ = [
    "RoleFlags",
    "StartupRefused",
    "code_head_revision",
    "ensure_database_ready",
    "ensure_deploy_environment",
    "role_problems",
]


class StartupRefused(RuntimeError):
    """시작 점검에 실패했다. 메시지에는 문제의 종류만 있다."""


def ensure_deploy_environment(settings: Settings, environ: Mapping[str, str] | None = None) -> None:
    """DB 없이 확인할 수 있는 점검. `environ`은 테스트에서 바꾼다(기본은 프로세스 환경 변수)."""
    env = os.environ if environ is None else environ
    if "RENDER" in env and settings.env == "dev":
        raise StartupRefused(
            "Render 환경(RENDER 환경 변수 있음)에서는 ORBIT_ENV=dev로 시작할 수 없다. "
            "개발용 로그인이 배포에서 열리지 않게 하기 위해서다"
        )
    if settings.env == "prod" and not settings.login_allowed_emails:
        raise StartupRefused(
            "ORBIT_ENV=prod이면 ORBIT_LOGIN_ALLOWED_EMAILS가 비어 있지 않아야 한다"
        )
    if settings.embedded_worker and settings.env == "prod" and settings.connector_mode != "google":
        raise StartupRefused(
            "ORBIT_ENV=prod에서 내장 워커는 ORBIT_CONNECTOR_MODE=google이 필요하다"
        )


def role_problems(report: RoleFlags) -> list[str]:
    """격리 조건에 어긋나는 항목을 설명한다(값 없이). 비어 있으면 통과."""
    problems: list[str] = []
    if report.is_superuser:
        problems.append("DB 접속 역할이 superuser이다")
    if report.bypasses_rls:
        problems.append("DB 접속 역할에 BYPASSRLS가 있다")
    if report.is_member_of_other_role:
        problems.append("DB 접속 역할이 다른 역할의 멤버이다(예: neon_superuser)")
    if report.owns_rls_table:
        problems.append("DB 접속 역할이 RLS를 켠 테이블을 소유한다")
    return problems


def code_head_revision(migrations_dir: Path | None = None) -> str:
    """코드의 최신 Alembic 리비전. 마이그레이션 디렉터리를 찾지 못하면 `StartupRefused`."""
    directory = migrations_dir if migrations_dir is not None else _default_migrations_dir()
    if not (directory / "versions").is_dir():
        raise StartupRefused("마이그레이션 디렉터리를 찾을 수 없어 DB 리비전을 확인할 수 없다")
    config = Config()
    config.set_main_option("script_location", str(directory))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise StartupRefused("마이그레이션 head가 하나가 아니다")
    return heads[0]


def _default_migrations_dir() -> Path:
    # backend/src/orbit/api/startup.py → backend/migrations (이미지도 같은 배치로 설치한다)
    return Path(__file__).resolve().parents[3] / "migrations"


async def ensure_database_ready(
    db: AsyncDatabase, settings: Settings, migrations_dir: Path | None = None
) -> None:
    """prod에서만 점검한다(dev·테스트 DB 경로는 건드리지 않는다)."""
    if settings.env != "prod":
        return
    problems = role_problems(await db.unscoped_startup_role_report())
    if problems:
        raise StartupRefused("; ".join(problems))
    head = code_head_revision(migrations_dir)
    current = await db.unscoped_alembic_version()
    if current is None:
        raise StartupRefused(
            "DB 리비전을 읽을 수 없다(alembic_version이 비었거나 orbit_app에 읽기 권한이 없다)"
        )
    if current != head:
        raise StartupRefused(
            "DB 마이그레이션이 코드의 최신 리비전과 다르다. "
            "소유자 역할로 alembic upgrade head를 먼저 실행한다"
        )
