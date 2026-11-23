"""rag-v1 검수 꾸러미(마크다운): 질문, 정답, 근거 메일 요약을 사람이 읽는다.

본문 발췌는 합성 데이터에서만 만든다. `bodies`가 없으면 구조(근거 ID·사실)만 적는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from orbit.evaldata.rag_v1.model import Plan
from orbit.evaldata.schema import MessageSetAnswer, Task
from orbit.evaldata.v1.skeleton import Skeleton

EXCERPT = 240


def _answer_text(t: Task) -> str:
    exp = t.gold.expected_answer
    if isinstance(exp, MessageSetAnswer):
        head = f"메일 {len(exp.messages)}통"
    else:
        head = "기권(메일에 없음)"
    facts = ", ".join(f"{f.description}={f.any_of[0]}" for f in t.gold.answer_facts)
    return f"{head}; {facts}" if facts else head


def render_review(
    plan: Plan,
    tasks: Sequence[Task],
    skeleton: Skeleton,
    bodies: Mapping[str, str] | None = None,
) -> str:
    subject = {m.id: m.subject for a in skeleton.accounts.values() for m in a.messages}
    bps = {bp.task_id: bp for bp in plan.blueprints}
    lines = [
        "# rag-v1 검수 꾸러미",
        "",
        f"- 시드 {plan.seed}, git `{plan.generator_git_sha[:12]}`, 스키마 `{plan.schema_version}`",
        f"- 문항 {len(tasks)}개, 본문 {'있음' if bodies else '없음(구조만)'}",
        "",
        "검수 질문: 질문이 한 가지로만 읽히는가 / 정답이 근거 메일로 확정되는가 / "
        "질문에 정답·중간 값이 드러나지 않는가",
        "",
    ]
    for t in tasks:
        bp = bps[t.task_id]
        lines += [
            f"## {t.task_id} ({t.split}, {bp.variant})",
            f"- 질문: {t.turns[0].content}",
            f"- 정답: {_answer_text(t)}",
            f"- 숨긴 중간 값: {', '.join(bp.hidden) or '없음'}",
        ]
        for m in bp.evidence:
            head = f"  - `{m}` {subject.get(m, '?')}"
            if bodies and m in bodies:
                text = bodies[m].replace("\n", " ")
                head += f" ({len(bodies[m])}자): {text[:EXCERPT]}"
            lines.append(head)
        lines.append("")
    return "\n".join(lines)
