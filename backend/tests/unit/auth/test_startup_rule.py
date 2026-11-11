"""시작 규칙과 로그인 설정 (ADR-0016 §1). DB 없이 앱 객체 생성까지만 확인한다."""

from pathlib import Path

import pytest
from pydantic import SecretStr

from google_mock import CLIENT_ID, CLIENT_SECRET, REDIRECT_URI, SIGNING_KEY
from orbit.api.app import create_app
from orbit.core.config import Settings

DB = SecretStr("postgresql+psycopg://x:y@localhost:1/z")  # 연결하지 않는다


def full(**overrides: object) -> Settings:
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


def paths(settings: Settings) -> set[str]:
    return set(create_app(settings).openapi()["paths"])


def test_non_dev_without_google_settings_refuses_to_start() -> None:
    with pytest.raises(RuntimeError) as info:
        create_app(Settings(database_url=DB, env="prod"))
    message = str(info.value)
    for name in (
        "ORBIT_GOOGLE_CLIENT_ID",
        "ORBIT_GOOGLE_CLIENT_SECRET",
        "ORBIT_GOOGLE_REDIRECT_URI",
        "ORBIT_SESSION_SIGNING_KEY",
        "ORBIT_LOGIN_ALLOWED_EMAILS",
    ):
        assert name in message


@pytest.mark.parametrize(
    ("override", "missing"),
    [
        ({"google_client_id": None}, "ORBIT_GOOGLE_CLIENT_ID"),
        ({"google_client_secret": None}, "ORBIT_GOOGLE_CLIENT_SECRET"),
        ({"google_redirect_uri": None}, "ORBIT_GOOGLE_REDIRECT_URI"),
        ({"google_redirect_uri": "http://orbit.test/cb"}, "ORBIT_GOOGLE_REDIRECT_URI"),
        ({"session_signing_key": None}, "ORBIT_SESSION_SIGNING_KEY"),
        ({"session_signing_key": SecretStr("too-short")}, "ORBIT_SESSION_SIGNING_KEY"),
        ({"login_allowed_emails": ""}, "ORBIT_LOGIN_ALLOWED_EMAILS"),
        ({"login_allowed_emails": " , "}, "ORBIT_LOGIN_ALLOWED_EMAILS"),
        ({"google_client_id": "  "}, "ORBIT_GOOGLE_CLIENT_ID"),
    ],
)
def test_non_dev_with_one_missing_setting_refuses_to_start(
    override: dict[str, object], missing: str
) -> None:
    with pytest.raises(RuntimeError) as info:
        create_app(full(**override))
    assert missing in str(info.value)
    assert CLIENT_SECRET not in str(info.value)
    assert SIGNING_KEY not in str(info.value)


def test_non_dev_with_complete_settings_starts_without_dev_login_route() -> None:
    found = paths(full())
    assert "/api/auth/google/start" in found
    assert "/api/auth/google/callback" in found
    assert "/api/dev/login" not in found
    assert "/api/session" in found


def test_dev_without_google_settings_starts_with_dev_login_only() -> None:
    found = paths(Settings(database_url=DB, env="dev"))
    assert "/api/dev/login" in found
    assert "/api/auth/google/start" not in found


def test_dev_with_partial_google_settings_does_not_open_google_login() -> None:
    found = paths(full(env="dev", login_allowed_emails=""))
    assert "/api/dev/login" in found
    assert "/api/auth/google/start" not in found


def test_dev_with_complete_settings_has_both() -> None:
    found = paths(full(env="dev", google_redirect_uri="http://localhost:5173/api/x"))
    assert "/api/dev/login" in found
    assert "/api/auth/google/start" in found


def test_signing_key_can_come_from_a_file(tmp_path: Path) -> None:
    key_file = tmp_path / "signing.key"
    key_file.write_text(SIGNING_KEY + "\n", encoding="utf-8")
    settings = full(session_signing_key=None, session_signing_key_file=str(key_file))
    assert settings.signing_key() == SIGNING_KEY.encode()
    assert "/api/auth/google/start" in paths(settings)


def test_signing_key_env_value_wins_over_file(tmp_path: Path) -> None:
    key_file = tmp_path / "signing.key"
    key_file.write_text("f" * 40, encoding="utf-8")
    settings = full(session_signing_key_file=str(key_file))
    assert settings.signing_key() == SIGNING_KEY.encode()


def test_unreadable_or_empty_key_file(tmp_path: Path) -> None:
    missing = full(session_signing_key=None, session_signing_key_file=str(tmp_path / "none"))
    with pytest.raises(RuntimeError):
        missing.signing_key()
    empty_file = tmp_path / "empty.key"
    empty_file.write_text("\n", encoding="utf-8")
    empty = full(session_signing_key=None, session_signing_key_file=str(empty_file))
    assert empty.signing_key() is None
    assert "ORBIT_SESSION_SIGNING_KEY" in empty.missing_google_login_settings()


def test_google_endpoints_must_be_https() -> None:
    with pytest.raises(ValueError, match="https"):
        full(google_token_url="http://mock.test/token")


def test_allowed_emails_are_split_and_lowercased() -> None:
    settings = full(login_allowed_emails=" Alice@Example.test, bob@example.test ,")
    assert settings.login_allowed_emails == ["alice@example.test", "bob@example.test"]


def test_secrets_do_not_appear_in_settings_repr() -> None:
    text = repr(full())
    assert CLIENT_SECRET not in text
    assert SIGNING_KEY not in text
