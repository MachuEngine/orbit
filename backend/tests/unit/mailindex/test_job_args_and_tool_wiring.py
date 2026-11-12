"""작업 인자, 도구 정의·프롬프트·설정 지문, 출처 분류 입력 (ADR-0019 §6·§8)."""

import json
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from orbit.actions.provenance import classify_emails
from orbit.assistant.envelope import safe_source, wrap_data
from orbit.assistant.tools import ReadOnlyTools, tool_specs
from orbit.connectors.sim.factory import build_sim_connectors
from orbit.connectors.sim.world import SimWorld
from orbit.core.clock import FixedClock
from orbit.core.config import Settings
from orbit.mailindex.jobs import validate_sync_args
from orbit.mailindex.limits import INDEX_CONFIG
from orbit.records.errors import InvalidJobArgs
from orbit.records.runs import RunMessageRecord
from orbit.runflow.fingerprint import config_fingerprint
from orbit.runflow.index_tool import SEARCH_INDEXED_MAIL, IndexedMailSearch
from orbit.runflow.prompt import PROMPT_VERSION, build_system_prompt
from orbit.runflow.transcript import Transcript
from orbit.runflow.write_tools import READ_TOOLS, WRITE_TOOLS, run_tool_specs

from .conftest import WORLD_PATH

WS, CID = str(uuid.uuid4()), str(uuid.uuid4())


def test_sync_args_accept_only_the_two_ids() -> None:
    assert validate_sync_args({"workspace_id": WS, "connection_id": CID}) == {
        "connection_id": CID,
        "workspace_id": WS,
    }
    for bad in (
        {"workspace_id": WS},
        {"workspace_id": WS, "connection_id": "not-a-uuid"},
        {"workspace_id": WS, "connection_id": CID, "query": "견적"},
        {"workspace_id": WS, "connection_id": 7},
        {"run_id": CID, "workspace_id": WS},
    ):
        with pytest.raises(InvalidJobArgs) as info:
            validate_sync_args(bad)
        assert "견적" not in str(info.value) and "not-a-uuid" not in str(info.value)


def test_indexed_search_is_in_run_tools_but_not_in_eval_tools() -> None:
    assert SEARCH_INDEXED_MAIL in READ_TOOLS and SEARCH_INDEXED_MAIL not in WRITE_TOOLS
    assert SEARCH_INDEXED_MAIL in [s.name for s in run_tool_specs()]
    assert SEARCH_INDEXED_MAIL not in [s.name for s in tool_specs()]  # 평가 경로(assistant_v0)


async def test_eval_tool_executor_still_rejects_the_indexed_search() -> None:
    world = SimWorld.load(WORLD_PATH, FixedClock(datetime(2026, 11, 11, tzinfo=UTC)))
    conns = build_sim_connectors(world, ["acct-alice"])
    tools = ReadOnlyTools(
        calendar=conns.calendar["acct-alice"], mail=conns.mail["acct-alice"], tz=ZoneInfo("UTC")
    )
    outcome = await tools.execute(SEARCH_INDEXED_MAIL, {"query": "견적"})
    assert json.loads(outcome.content) == {"error": "unknown_tool"}


def test_tool_spec_forbids_extra_arguments_and_bounds_results() -> None:
    spec = next(s for s in run_tool_specs() if s.name == SEARCH_INDEXED_MAIL)
    assert spec.parameters["required"] == ["query"]
    assert spec.parameters["additionalProperties"] is False
    assert spec.parameters["properties"]["max_results"]["maximum"] == 20


@pytest.mark.parametrize(
    "arguments",
    [
        None,
        {},
        {"query": ""},
        {"query": "  "},
        {"query": 3},
        {"query": "a", "x": 1},
        {"query": "a", "max_results": 0},
        {"query": "a", "max_results": 21},
        {"query": "a", "max_results": True},
        {"query": "a" * 501},
    ],
)
def test_argument_validation_rejects_bad_input(arguments: object) -> None:
    assert IndexedMailSearch._parse(arguments) is None  # noqa: SLF001


def test_argument_validation_defaults() -> None:
    assert IndexedMailSearch._parse({"query": " 견적 "}) == ("견적", 5)  # noqa: SLF001
    assert IndexedMailSearch._parse({"query": "a", "max_results": 20}) == ("a", 20)  # noqa: SLF001


def test_prompt_v0_5_explains_both_mail_tools_and_freshness() -> None:
    assert PROMPT_VERSION == "runflow-v0.5"
    prompt = build_system_prompt(datetime(2026, 11, 12, tzinfo=UTC), UTC, "0123456789ab")
    assert "search_mail" in prompt and "search_indexed_mail" in prompt
    assert "N분 전 기준" in prompt and "index_empty" in prompt


def test_fingerprint_includes_embedding_model_and_index_constants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = config_fingerprint("m")
    assert base == config_fingerprint("m", embedding_model_id="text-embedding-3-small")
    assert base != config_fingerprint("m", embedding_model_id="other-embedding")
    monkeypatch.setitem(INDEX_CONFIG, "rrf_k", 61)
    assert config_fingerprint("m") != base


def _tool_row(text: str) -> RunMessageRecord:
    content = {"step": 0, "tool_call_id": "c1", "name": SEARCH_INDEXED_MAIL, "text": text}
    return RunMessageRecord(1, "tool", content, datetime(2026, 11, 11, tzinfo=UTC))


RESULT = json.dumps(
    {
        "results": [
            {
                "handle": "M1",
                "subject": "견적 문의",
                "from": {"name": "밥", "email": "bob@example.org"},
                "date": "2026-11-05T18:00:00+09:00",
                "excerpt": "담당자는 carol@example.net 입니다",
                "excerpt_truncated": False,
            }
        ],
        "indexed_as_of": "2026-11-11T18:00:00+09:00",
        "indexed_minutes_ago": 3,
    },
    ensure_ascii=False,
)


def test_indexed_result_is_workspace_data_for_senders_and_external_for_excerpt() -> None:
    transcript = Transcript((_tool_row(RESULT),))
    workspace_emails, external_texts = transcript.provenance_inputs()
    assert workspace_emails == ["bob@example.org"]
    assert any("carol@example.net" in t for t in external_texts)
    provenance = classify_emails(
        ["bob@example.org", "carol@example.net", "dave@example.net"],
        user_utterances=["메일 보내줘"],
        workspace_emails=workspace_emails,
        external_texts=external_texts,
    )
    assert provenance == {
        "bob@example.org": "workspace_data",
        "carol@example.net": "external_content",
        "dave@example.net": "model",
    }


def test_indexed_result_is_wrapped_in_the_data_envelope() -> None:
    wrapped = wrap_data(RESULT, source=safe_source(SEARCH_INDEXED_MAIL), boundary="0123456789ab")
    assert wrapped.startswith('<orbit_data source="search_indexed_mail" boundary="0123456789ab">')
    assert "<" not in wrapped.split("\n", 1)[1].rsplit("\n", 1)[0]  # 본문 속 꺾쇠는 이스케이프된다


def test_mail_index_settings_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORBIT_DATABASE_URL", "postgresql+psycopg://u:pw@h/db")
    for name in (
        "ORBIT_EMBEDDING_PROVIDER",
        "ORBIT_EMBEDDING_MODEL_ID",
        "ORBIT_MAIL_INDEX_SYNC_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.embedding_provider == "openai"
    assert settings.embedding_model_id == "text-embedding-3-small"
    assert settings.mail_index_sync_interval_seconds == 900
    assert settings.mail_index_query_stale_seconds == 300
    assert settings.mail_index_query_sync_timeout_seconds == 10
    monkeypatch.setenv("ORBIT_EMBEDDING_PROVIDER", "fake")
    monkeypatch.setenv("ORBIT_MAIL_INDEX_QUERY_STALE_SECONDS", "60")
    assert Settings().embedding_provider == "fake"
    assert Settings().mail_index_query_stale_seconds == 60
