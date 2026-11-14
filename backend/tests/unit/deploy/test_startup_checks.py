"""시작 시 안전 점검 중 DB 없이 확인하는 부분 (ADR-0022 §3·§5). 값은 모두 합성이다."""

import re
from pathlib import Path

import pytest
from pydantic import SecretStr

from google_mock import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SIGNING_KEY
from orbit.api.app import create_app
from orbit.api.startup import (
    StartupRefused,
    code_head_revision,
    ensure_deploy_environment,
    role_problems,
)
from orbit.core.config import Settings
from orbit.db.session import RoleFlags

DB = SecretStr("postgresql+psycopg://x:y@localhost:1/z")  # 연결하지 않는다
VERSIONS = Path(__file__).resolve().parents[3] / "migrations" / "versions"


def prod(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "database_url": DB,
        "env": "prod",
        "google_client_id": CLIENT_ID,
        "google_client_secret": SecretStr(CLIENT_SECRET),
        "google_redirect_uri": REDIRECT_URI,
        "session_signing_key": SecretStr(SIGNING_KEY),
        "login_allowed_emails": "alice@example.test",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_render_environment_with_dev_env_is_refused() -> None:
    settings = Settings(database_url=DB, env="dev")
    with pytest.raises(StartupRefused, match="RENDER") as info:
        ensure_deploy_environment(settings, {"RENDER": "true"})
    assert "ORBIT_ENV=dev" in str(info.value)
    ensure_deploy_environment(settings, {})  # RENDER가 없으면 dev는 그대로 시작한다


def test_create_app_refuses_dev_on_render(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(StartupRefused, match="RENDER"):
        create_app(Settings(database_url=DB, env="dev"))
    assert create_app(prod())  # prod는 RENDER가 있어도 시작한다


def test_prod_with_empty_login_allowlist_is_refused() -> None:
    with pytest.raises(StartupRefused, match="ORBIT_LOGIN_ALLOWED_EMAILS"):
        ensure_deploy_environment(prod(login_allowed_emails=""), {})
    ensure_deploy_environment(prod(), {})
    # create_app 경로에서는 로그인 설정 점검(ensure_startable)이 먼저 같은 이유로 거부한다
    with pytest.raises(RuntimeError, match="ORBIT_LOGIN_ALLOWED_EMAILS"):
        create_app(prod(login_allowed_emails=""))


def test_embedded_worker_in_prod_requires_google_connector() -> None:
    with pytest.raises(StartupRefused, match="ORBIT_CONNECTOR_MODE=google"):
        ensure_deploy_environment(prod(embedded_worker=True, connector_mode="sim"), {})
    ensure_deploy_environment(prod(embedded_worker=True, connector_mode="google"), {})
    with pytest.raises(StartupRefused, match="ORBIT_CONNECTOR_MODE=google"):
        create_app(prod(embedded_worker=True, connector_mode="sim"))  # create_app에서도 거부한다
    # dev에서는 sim도 허용한다
    ensure_deploy_environment(Settings(database_url=DB, env="dev", embedded_worker=True), {})


@pytest.mark.parametrize(
    ("flags", "fragment"),
    [
        (RoleFlags(True, False, False, False), "superuser"),
        (RoleFlags(False, True, False, False), "BYPASSRLS"),
        (RoleFlags(False, False, True, False), "멤버"),
        (RoleFlags(False, False, False, True), "소유"),
    ],
)
def test_each_role_problem_is_reported_without_values(flags: RoleFlags, fragment: str) -> None:
    problems = role_problems(flags)
    assert len(problems) == 1 and fragment in problems[0]


def test_clean_role_has_no_problem_and_all_problems_accumulate() -> None:
    assert role_problems(RoleFlags(False, False, False, False)) == []
    assert len(role_problems(RoleFlags(True, True, True, True))) == 4


def test_code_head_matches_newest_migration_file() -> None:
    revisions = {
        m.group(1)
        for path in VERSIONS.glob("*.py")
        if (m := re.search(r'^revision = "([^"]+)"', path.read_text(encoding="utf-8"), re.M))
    }
    downs = {
        m.group(1)
        for path in VERSIONS.glob("*.py")
        if (m := re.search(r'^down_revision = "([^"]+)"', path.read_text(encoding="utf-8"), re.M))
    }
    assert code_head_revision() == (revisions - downs).pop()


def test_missing_migrations_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StartupRefused, match="마이그레이션 디렉터리"):
        code_head_revision(tmp_path / "none")
