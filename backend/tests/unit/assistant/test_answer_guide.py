"""답 제출 안내 통일(eval-answer-v2)과 새 정답 유형 제출 (ADR-0026 §4·§7). 합성 데이터만 쓴다."""

import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from orbit.assistant.answer_guide import answer_guide_lines
from orbit.assistant.prompt import CLARIFY_LINE
from orbit.assistant.prompt import build_system_prompt as assistant_prompt
from orbit.assistant.tools import ANSWER_KINDS, ReadOnlyTools, tool_specs
from orbit.runflow.answer import (
    ANSWER_EXTENSION_VERSION,
    ANSWER_PROMPT_PARAGRAPH,
    answer_tool_spec,
    with_answer_prompt,
)
from orbit.runflow.prompt import build_system_prompt as runflow_prompt

from .conftest import DAY

NOW = datetime(2026, 11, 11, 0, 0, tzinfo=UTC)
TZ = ZoneInfo("Asia/Seoul")
GUIDE = "\n".join(answer_guide_lines())


def test_both_paths_embed_byte_identical_guide() -> None:
    assert GUIDE in assistant_prompt(NOW, TZ, "B1")
    assert GUIDE in with_answer_prompt(runflow_prompt(NOW, TZ, "B1"))
    assert ANSWER_PROMPT_PARAGRAPH == "[답 제출]\n" + GUIDE + "\n"
    assert ANSWER_EXTENSION_VERSION == "eval-answer-v2"


def test_guide_lists_every_kind_with_an_example() -> None:
    lines = answer_guide_lines()
    for kind in ANSWER_KINDS:
        entry = [line for line in lines if line.startswith(f"  - {kind}:")]
        assert len(entry) == 1, kind
    for kind in ("event_set", "event", "message_set", "document", "abstain"):
        first = next(line for line in lines if line.startswith(f"  - {kind}:"))
        assert "예:" in first, kind
    # 되물을 때 clarify를 제출하고 같은 응답에 질문을 적는다
    clarify = next(line for line in lines if line.startswith("  - clarify:"))
    assert "같은 응답" in clarify and "되묻는 질문" in clarify
    assert "오류를 돌려주면 인자를 고쳐 다시 호출" in GUIDE
    assert "도구를 더 부르지 않고" in GUIDE


def test_clarify_line_is_in_product_runflow_prompt_without_the_eval_extension() -> None:
    product = runflow_prompt(NOW, TZ, "B1")
    assert CLARIFY_LINE in product
    assert "두 가지 이상으로 읽히고" in CLARIFY_LINE and "되묻지 않는다" in CLARIFY_LINE
    assert "[답 제출]" not in product and "submit_answer" not in product
    assert CLARIFY_LINE in assistant_prompt(NOW, TZ, "B1")
    # 기존 "후보가 둘 이상이면 되묻는다" 줄은 그대로다
    assert "후보가 둘 이상이면 되묻는다." in product


def test_submit_answer_spec_lists_new_kinds_and_fields() -> None:
    spec = next(s for s in tool_specs() if s.name == "submit_answer")
    props = spec.parameters["properties"]
    assert set(props["kind"]["enum"]) == set(ANSWER_KINDS)
    assert {"message_set", "document", "abstain", "clarify"} <= set(ANSWER_KINDS)
    assert props["messages"] == {"type": "array", "items": {"type": "string"}}
    assert props["document"] == {"type": "string"}
    assert answer_tool_spec() == spec


async def submit(tools: ReadOnlyTools, **args: Any) -> Any:
    return await tools.execute("submit_answer", args)


async def test_submit_message_set_resolves_handles_and_dedups(tools: ReadOnlyTools) -> None:
    await tools.execute("search_mail", {"query": "from:bob@example.org"})
    m1 = tools.workset.resolve_message("M1")
    m2 = tools.workset.resolve_message("M2")
    out = await submit(tools, kind="message_set", messages=["M2", "M1", "M2"])
    assert out.structured == {
        "kind": "message_set",
        "messages": [{"message_id": m2}, {"message_id": m1}],
    }
    empty = await submit(tools, kind="message_set", messages=[])
    assert empty.structured == {"kind": "message_set", "messages": []}


async def test_submit_document_resolves_handle(tools: ReadOnlyTools) -> None:
    handle = tools.workset.document_handle("file-synthetic-1")
    out = await submit(tools, kind="document", document=handle)
    assert out.structured == {"kind": "document", "document": {"file_id": "file-synthetic-1"}}


async def test_submit_abstain_and_clarify_carry_only_kind(tools: ReadOnlyTools) -> None:
    await tools.execute("list_events", dict(DAY))
    for kind in ("abstain", "clarify"):
        out = await submit(tools, kind=kind, events=["E1"], event="E1", start="x")
        assert out.structured == {"kind": kind}
        assert not out.is_error


async def test_submit_unknown_or_malformed_new_kind_handles_are_errors(
    tools: ReadOnlyTools,
) -> None:
    await tools.execute("search_mail", {"query": "from:bob@example.org"})
    cases: list[tuple[dict[str, Any], str]] = [
        ({"kind": "message_set", "messages": ["M1", "M99"]}, "unknown_handle"),
        ({"kind": "message_set", "messages": ["E1"]}, "unknown_handle"),
        ({"kind": "message_set"}, "invalid_arguments"),
        ({"kind": "message_set", "messages": "M1"}, "invalid_arguments"),
        ({"kind": "document", "document": "D1"}, "unknown_handle"),  # 문서 핸들을 얻지 못했다
        ({"kind": "document"}, "unknown_handle"),
    ]
    for args, kind in cases:
        outcome = await tools.execute("submit_answer", args)
        assert outcome.structured is None and outcome.is_error, args
        assert json.loads(outcome.content) == {"error": kind}, args


def test_versions_and_fingerprints_reflect_new_prompts() -> None:
    from orbit.assistant.prompt import PROMPT_VERSION as ASSISTANT_VERSION
    from orbit.evalrunner.suts import create_sut
    from orbit.runflow.fingerprint import config_fingerprint
    from orbit.runflow.prompt import PROMPT_VERSION as RUNFLOW_VERSION

    assert ASSISTANT_VERSION == "assistant-v0.4" and RUNFLOW_VERSION == "runflow-v0.8"
    assert create_sut("assistant_v0").version == "0+assistant-v0.4+temp0"
    assert create_sut("runflow_v0").version == "0+runflow-v0.8+eval-answer-v2+index-off"
    # 확장을 켜면 지문이 달라지고, 끄면 확장이 없던 때와 같다(안내 문구 변경은 지문에 반영된다)
    assert config_fingerprint("m", None) != config_fingerprint("m", None, answer_extension=True)
