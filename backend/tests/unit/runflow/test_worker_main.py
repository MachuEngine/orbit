"""개발용 워커 진입점: LLM 설정이 없으면 추적 대신 한 줄 안내로 끝난다."""

import pytest

from orbit.runflow.worker import main


def test_worker_exits_with_hint_when_llm_settings_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORBIT_DATABASE_URL", "postgresql+psycopg://u:p@localhost:1/none")
    monkeypatch.setenv("ORBIT_ENV", "dev")
    monkeypatch.setenv("ORBIT_SIM_SNAPSHOT_PATH", "unused.json")
    for name in ("ORBIT_LLM_MODEL_ID", "ORBIT_LLM_PROVIDER_ORDER", "ORBIT_OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as exc:
        main()

    message = str(exc.value)
    assert "ORBIT_LLM_MODEL_ID" in message
    assert "ORBIT_LLM_PROVIDER_ORDER" in message
    assert "ORBIT_OPENROUTER_API_KEY" in message
    assert "\n" not in message
