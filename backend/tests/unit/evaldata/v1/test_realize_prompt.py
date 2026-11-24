"""본문 실현 프롬프트 `realize-v3`: 필수 사실은 표기 하나만, 금지 사실은 모든 표기를 보인다."""

from orbit.evaldata.v1.realize_prompt import build_user
from orbit.evaldata.v1.skeleton import BodyContext, BodySpec, Fact

FORMS = ["11월 19일", "11/19", "11월 19일(목)"]


def _spec() -> BodySpec:
    return BodySpec.model_validate(
        {
            "artifact": "mail",
            "genre": "colleague",
            "length_class": "medium",
            "target_chars": (200, 700),
            "context": BodyContext(),
            "required_facts": [Fact(fact_id="f1", kind="deadline", value="x", surface_forms=FORMS)],
            "forbidden_facts": [
                Fact(fact_id="f2", kind="item", value="y", surface_forms=["금지A", "금지B"])
            ],
            "template_text": "템플릿",
            "generator_family": "openai",
        }
    )


def _required_line(text: str) -> str:
    lines = text.splitlines()
    return lines[lines.index("필수 사실(표기를 그대로 넣는다):") + 1]


def test_required_fact_shows_single_form_and_is_deterministic() -> None:
    spec = _spec()
    line = _required_line(build_user(spec, body_id="b1"))
    assert " | " not in line
    assert line.removeprefix("- [deadline] ") in FORMS
    assert _required_line(build_user(spec, body_id="b1")) == line


def test_form_varies_across_bodies() -> None:
    spec = _spec()
    seen = {_required_line(build_user(spec, body_id=f"b{i}")) for i in range(30)}
    assert len(seen) == len(FORMS)


def test_forbidden_fact_keeps_all_forms() -> None:
    assert "- [item] 금지A | 금지B" in build_user(_spec(), body_id="b1")
