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


def test_configure_logging_uses_level_and_format(monkeypatch: pytest.MonkeyPatch) -> None:
    import logging

    from orbit.core.config import Settings
    from orbit.runflow import worker

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: calls.append(kw))
    monkeypatch.setenv("ORBIT_DATABASE_URL", "postgresql+psycopg://u:p@localhost:1/none")
    monkeypatch.setenv("ORBIT_LOG_LEVEL", "warning")
    worker.configure_logging(Settings().log_level)
    assert calls == [{"level": "WARNING", "format": worker.LOG_FORMAT}]
    assert worker.LOG_FORMAT == "%(asctime)s %(levelname)s %(name)s %(message)s"


@pytest.mark.parametrize("level", ["INFO", "DEBUG"])
def test_configure_logging_silences_httpx_request_urls(
    level: str, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from orbit.runflow import worker

    names = worker.QUIET_LOGGERS
    saved = [logging.getLogger(n).level for n in names]
    try:
        worker.configure_logging(level)
        caplog.set_level(logging.DEBUG)
        for n in names:
            logging.getLogger(n).info("GET https://example.test/search?q=synthetic")
            logging.getLogger(n).debug("send_request_headers")
            assert logging.getLogger(n).getEffectiveLevel() == logging.WARNING
        assert caplog.records == []
    finally:
        for n, lv in zip(names, saved, strict=True):
            logging.getLogger(n).setLevel(lv)
