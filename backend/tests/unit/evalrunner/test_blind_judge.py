"""실제 표본을 커밋하지 않고 작은 합성 사실로 blind60의 불변식을 검증한다."""

import json
from html.parser import HTMLParser
from typing import Any

import pytest

from orbit.evalrunner import blind_judge as b
from orbit.evalrunner import judge as j


def sources() -> list[j.Episode]:
    return [
        j.Episode(
            f"source-sut/secret-source-{index}/1",
            f"secret-source-{index}",
            1,
            "source-sut",
            "mail",
            f"cluster-{index}",
            "담당자와 회의 날짜를 알려줘.",
            f"담당자는 가상담당자{index}이며 회의는 11월 17일입니다.",
            (
                (
                    "read_mail",
                    json.dumps(
                        {
                            "name": f"가상담당자{index}",
                            "date": "2026-11-17",
                            "budget": "9,100,000원",
                        },
                        ensure_ascii=False,
                    ),
                ),
            ),
        )
        for index in range(60)
    ]


def candidates(episodes: list[j.Episode]) -> list[dict[str, Any]]:
    return [
        {
            "source_id": ep.episode_id,
            "old_value": f"가상담당자{index}",
            "new_value": f"가상대체자{index}",
            "kind": "name",
            "reviewed": True,
        }
        for index, ep in enumerate(episodes[:38])
    ]


def bundle() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    episodes = sources()
    return b.generate(episodes, {ep.episode_id: True for ep in episodes}, candidates(episodes))


@pytest.mark.parametrize(
    "answer,tool",
    [
        ("910만 원", "9,100,000"),
        ("9,100,000원", "910만원"),
        ("1.5억 원", "150,000,000원"),
        ("2026년 11월 17일", "2026-11-17"),
        ("11월 17일", "2026-11-17T09:00:00+09:00"),
        ("오후 2시 30분", "2026-11-17T05:30:00Z"),
    ],
)
def test_semantically_equal_fact_formats(answer: str, tool: str) -> None:
    kind = "date" if "월" in answer else "time" if "시" in answer else "money"
    assert b.fact_present(answer, kind, tool)


def test_normalized_existing_new_value_is_rejected() -> None:
    episode = j.Episode(
        "source/a/1",
        "a",
        1,
        "source",
        None,
        "c",
        "예산은?",
        "예산은 830만 원입니다.",
        (("read_mail", "8,300,000원과 9,100,000원"),),
    )
    mutation = b.Mutation(episode.episode_id, "830만 원", "910만 원", "money")
    with pytest.raises(b.BlindError, match="새 사실이 도구에 이미 있다"):
        b.validate_mutation(episode, mutation)


def test_old_fact_must_be_supported_and_visible_to_judge() -> None:
    episode = j.Episode(
        "source/a/1",
        "a",
        1,
        "source",
        None,
        "c",
        "담당자는?",
        "담당자는 가상담당자입니다.",
        (("read_mail", "x" * 13000 + "가상담당자"),),
    )
    mutation = b.Mutation(episode.episode_id, "가상담당자", "가상대체자", "name")
    with pytest.raises(b.BlindError, match="Judge 발췌 밖"):
        b.validate_mutation(episode, mutation)


def test_sixty_unique_sources_balanced_and_seeded() -> None:
    episodes = sources()
    human = {ep.episode_id: True for ep in episodes}
    manifest = candidates(episodes)
    inputs, keys = b.generate(episodes, human, manifest)
    assert len(inputs) == len(keys) == 60
    assert sum(key["mutated"] for key in keys) == 30
    assert len({key["source_id"] for key in keys}) == 60
    assert {key["source_id"] for key in keys} == set(human)
    assert len({(row["sut"], row["task_id"], row["repeat"]) for row in inputs}) == 60
    assert all(row["sut"] == "blind" and row["repeat"] == 1 for row in inputs)
    assert (inputs, keys) == b.generate(list(reversed(episodes)), human, list(reversed(manifest)))
    assert (inputs, keys) != b.generate(episodes, human, manifest, seed=20261118)
    originals = {ep.episode_id: ep.answer for ep in episodes}
    for row, key in zip(inputs, keys, strict=True):
        expected = originals[key["source_id"]]
        if key["mutated"]:
            expected = expected.replace(key["old_value"], key["new_value"], 1)
        assert row["answer_text"] == expected


def test_unreviewed_candidate_and_non_grounded_source_are_rejected() -> None:
    episodes = sources()
    human = {ep.episode_id: True for ep in episodes}
    manifest = candidates(episodes)
    manifest[0]["reviewed"] = False
    with pytest.raises(b.BlindError, match="검토 확인"):
        b.generate(episodes, human, manifest)
    manifest[0]["reviewed"] = True
    human[episodes[-1].episode_id] = False
    with pytest.raises(b.BlindError, match="원본 60개 모두"):
        b.generate(episodes, human, manifest)


class DataScript(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inside = False
        self.payload = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.inside = tag == "script" and dict(attrs).get("id") == "review-data"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.inside = False

    def handle_data(self, data: str) -> None:
        if self.inside:
            self.payload += data


def test_html_projection_has_no_key_or_original_ids_and_escapes_xss() -> None:
    inputs, keys = bundle()
    attack = '</script><img src="https://example.invalid/collect" onerror="alert(1)">__STORAGE__'
    inputs[0].update(
        {
            "source_id": "DO-NOT-SHOW-SOURCE",
            "mutated": "DO-NOT-SHOW-KEY",
            "grounded": "DO-NOT-SHOW-LABEL",
            "prediction": "DO-NOT-SHOW-PREDICTION",
        }
    )
    inputs[0]["answer_text"] += attack
    html = b.render_html(inputs, ":root { color-scheme: dark; }")
    assert all(key["source_id"] not in html for key in keys)
    assert "DO-NOT-SHOW" not in html
    assert "<img" not in html and html.count("</script>") == 2
    assert "innerHTML" not in html and "textContent" in html
    assert "connect-src 'none'" in html and "img-src 'none'" in html
    parser = DataScript()
    parser.feed(html)
    projected = json.loads(parser.payload)
    assert projected[0]["answer"].endswith(attack)
    assert set(projected[0]) == {"n", "question", "answer", "hints", "tools"}
    assert all(set(tool) == {"name", "result"} for tool in projected[0]["tools"])


def test_hints_compare_money_without_creating_judgment() -> None:
    tools = [{"name": "read_mail", "result": '{"budget": "9,100,000원"}'}]
    hints = b.evidence_hints("예산은 910만 원입니다.", tools)
    assert any("9,100,000" in hint for hint in hints)
    assert all("오경보" not in hint and "unsupported" not in hint for hint in hints)


def test_hints_exclude_zero_overlap_lines_instead_of_filling_six_slots() -> None:
    tools = [
        {
            "name": "get_event",
            "result": json.dumps(
                {
                    "events": [
                        {
                            "title": "합성 점검회의",
                            "all_day": False,
                            "version": "synthetic-version",
                            "location": "다른 회의실",
                            "description": "별도 설명",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        }
    ]
    # JSON 배열 경로의 [0]은 답의 00분과 숫자가 겹쳐도 근거로 세지 않는다.
    hints = b.evidence_hints("합성 점검회의를 10시 00분에 찾았습니다.", tools)
    assert len(hints) == 1 and "title" in hints[0]
    assert not any("all_day" in hint or "version" in hint for hint in hints)
    assert b.evidence_hints("출장 예산을 알려드립니다.", tools) == []
    html = b.render_html([{"answer_text": "출장 예산을 알려드립니다.", "tool_results": tools}], "")
    assert "답변과 겹치는 근거 후보가 없습니다. 전체 도구 결과를 확인해 주세요." in html


def test_missing_predictions_keep_sixty_and_thirty_denominators() -> None:
    _, keys = bundle()
    predictions: dict[str, bool | None] = {key["id"]: not key["mutated"] for key in keys}
    original = next(key for key in keys if not key["mutated"])
    mutated = next(key for key in keys if key["mutated"])
    del predictions[original["id"]]
    predictions[mutated["id"]] = None
    report = b.score_key(keys, predictions)
    assert report["n"] == 60 and report["accuracy"] == pytest.approx(58 / 60)
    assert report["supported_recall"] == report["unsupported_recall"] == pytest.approx(29 / 30)
    assert (
        report["missing"] == 2 and report["missing_supported"] == report["missing_unsupported"] == 1
    )
    assert report["false_alarm_rate"] == 0 and report["human_kappa"] == "pending"
    predictions[original["id"]] = False
    assert b.score_key(keys, predictions)["false_alarm_rate"] == pytest.approx(1 / 30)


def test_key_size_and_balance_cannot_be_silently_weakened() -> None:
    _, keys = bundle()
    with pytest.raises(b.BlindError, match="60문항"):
        b.score_key(keys[:-1], {})
    keys[0]["mutated"] = not keys[0]["mutated"]
    with pytest.raises(b.BlindError, match="원본 30개"):
        b.score_key(keys, {})


def test_remote_resources_in_tokens_are_rejected() -> None:
    inputs, _ = bundle()
    with pytest.raises(b.BlindError, match="외부 리소스"):
        b.render_html(inputs, '@import "https://example.invalid/font.css";')
