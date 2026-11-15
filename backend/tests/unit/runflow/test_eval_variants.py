"""평가 전용 원인 확인 변형 (ADR-0023 §10). 데이터는 모두 합성 값이다."""

import hashlib
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from orbit.assistant.prompt import build_system_prompt as assistant_prompt
from orbit.evalrunner.runflow_sut import RunflowV0SUT
from orbit.runflow.eval_variants import (
    FACTS_EXTENSION_VERSION,
    FACTS_PROMPT_PARAGRAPH,
    ReasoningCache,
    with_facts_prompt,
)
from orbit.runflow.fingerprint import config_fingerprint
from orbit.runflow.prompt import PROMPT_VERSION
from orbit.runflow.prompt import build_system_prompt as runflow_prompt

NOW = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)
TZ = ZoneInfo("Asia/Seoul")
INFO = {"provider": "openai", "reasoning_items_roundtrip": True}


def test_assistant_prompt_is_byte_identical_after_line_sharing() -> None:
    # 줄을 함수로 꺼내기 전 출력의 SHA-256(assistant-v0.3, 경계값 B123)
    digest = hashlib.sha256(assistant_prompt(NOW, TZ, "B123").encode()).hexdigest()
    assert digest == "cf63a01639c225592f779fbf9593371d366b7265b3ab56ad1586554cb447e372"
    digest = hashlib.sha256(runflow_prompt(NOW, TZ, "B123").encode()).hexdigest()
    assert digest == "1b67cc44026a019c2c51e6ac3e424c9c71d1875137c4c16de0589d3a68ee3b28"


def test_facts_paragraph_shares_lines_with_assistant_prompt() -> None:
    base = assistant_prompt(NOW, TZ, "B123")
    for line in FACTS_PROMPT_PARAGRAPH.splitlines():
        # 하루 조회 범위 규칙은 assistant 프롬프트에서 시각 형식 줄의 뒷문장이다
        assert line.removeprefix("- ") in base if "하루는" in line else line in base
    assert "[사실 확인]" in FACTS_PROMPT_PARAGRAPH
    assert "하루는 그날 00:00부터 다음 날 00:00까지로 조회한다." in FACTS_PROMPT_PARAGRAPH


def test_with_facts_prompt_appends_paragraph_only() -> None:
    base = runflow_prompt(NOW, TZ, "B123")
    assert "[사실 확인]" not in base
    assert with_facts_prompt(base) == base + "\n" + FACTS_PROMPT_PARAGRAPH


def test_default_fingerprint_is_unchanged_and_variants_differ() -> None:
    for info in (None, INFO):
        plain = config_fingerprint("m", info)
        assert plain == config_fingerprint(
            "m", info, facts_extension=False, reasoning_roundtrip=False
        )
        facts = config_fingerprint("m", info, facts_extension=True)
        rt = config_fingerprint("m", info, reasoning_roundtrip=True)
        both = config_fingerprint("m", info, facts_extension=True, reasoning_roundtrip=True)
        assert len({plain, facts, rt, both}) == 4


def test_reasoning_roundtrip_fingerprint_records_actual_llm_value() -> None:
    off = {**INFO, "reasoning_items_roundtrip": False}  # 추론 강도 none
    assert config_fingerprint("m", INFO, reasoning_roundtrip=True) != config_fingerprint(
        "m", off, reasoning_roundtrip=True
    )
    # 끄면 llm_info 값과 무관하게 워커 경로와 같은 False로 기록한다
    assert config_fingerprint("m", INFO) == config_fingerprint("m", off)


def test_sut_version_and_config_info_list_variants() -> None:
    base = f"0+{PROMPT_VERSION}+eval-answer-v1+index-off"
    assert RunflowV0SUT().version == base
    assert RunflowV0SUT(variants=["reasoning-rt", "facts"]).version == (
        f"{base}+{FACTS_EXTENSION_VERSION}+reasoning-rt"
    )
    assert RunflowV0SUT(variants=["reasoning-rt"]).version == f"{base}+reasoning-rt"
    plain = RunflowV0SUT().config_info(model_id="m", llm_info=dict(INFO))
    both = RunflowV0SUT(variants=["reasoning-rt", "facts"]).config_info(
        model_id="m", llm_info=dict(INFO)
    )
    assert plain["variants"] == []
    assert both["variants"] == ["facts-v1", "reasoning-rt"]
    assert plain["runflow_fingerprint"] == config_fingerprint("m", INFO, answer_extension=True)
    assert both["runflow_fingerprint"] != plain["runflow_fingerprint"]
    with pytest.raises(ValueError):
        RunflowV0SUT(variants=["nope"])


def test_reasoning_cache_is_scoped_by_run_and_skips_empty() -> None:
    cache = ReasoningCache()
    cache.put("run-a", 0, ({"type": "reasoning", "id": "r0"},))
    cache.put("run-a", 1, ())
    cache.put("run-b", 0, ({"type": "reasoning", "id": "r9"},))
    assert cache.for_run("run-a") == {0: ({"type": "reasoning", "id": "r0"},)}
    assert cache.for_run("run-c") == {}
