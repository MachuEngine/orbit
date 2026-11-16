"""`skeleton-stats.json`: 계정별 수량, 계열별 본문 수·글자 수, 시나리오 분포, (c) 비용 추정 근거."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from pydantic import BaseModel

from orbit.evaldata.v1.skeleton import ArtifactRef, Skeleton
from orbit.evaldata.v1.worldmodel import WorldModel


def _body_ids(skeleton: Skeleton) -> set[str]:
    return set(skeleton.bodies)


def skeleton_stats(wm: WorldModel, sk: Skeleton) -> dict[str, Any]:
    accounts: dict[str, Any] = {}
    for acct, a in sk.accounts.items():
        accounts[acct] = {
            "purpose": a.purpose,
            "time_zone": a.time_zone,
            "calendars": len(a.calendars),
            "events": len(a.events),
            "all_day_events": sum(1 for e in a.events if e.all_day),
            "cancelled_events": sum(1 for e in a.events if e.status == "cancelled"),
            "messages": len(a.messages),
            "threads": len(a.threads),
            "files": len(a.files),
        }
    unique = Counter(spec.artifact for spec in sk.bodies.values())
    length = {art: Counter[str]() for art in ("mail", "document")}
    mid_sum = 0.0
    mid_by_artifact: Counter[str] = Counter()
    fam: dict[str, dict[str, Any]] = {}
    for spec in sk.bodies.values():
        length[spec.artifact][spec.length_class] += 1
        mid = (spec.target_chars[0] + spec.target_chars[1]) / 2
        mid_sum += mid
        mid_by_artifact[spec.artifact] += int(mid)
        f = fam.setdefault(
            spec.generator_family,
            {
                "bodies": Counter(),
                "target_chars_min_sum": 0,
                "target_chars_max_sum": 0,
                "target_chars_midpoint_sum": 0.0,
                "length_class": Counter(),
            },
        )
        f["bodies"][spec.artifact] += 1
        f["target_chars_min_sum"] += spec.target_chars[0]
        f["target_chars_max_sum"] += spec.target_chars[1]
        f["target_chars_midpoint_sum"] += mid
        f["length_class"][spec.length_class] += 1
    types_by_family: dict[str, Counter[str]] = defaultdict(Counter)
    per_type: Counter[str] = Counter()
    for s in sk.scenarios:
        types_by_family[s.generator_family][s.type] += 1
        per_type[s.type] += 1
    families = {}
    for name, f in sorted(fam.items()):
        families[name] = {
            "bodies": dict(f["bodies"]),
            "bodies_total": sum(f["bodies"].values()),
            "target_chars_min_sum": f["target_chars_min_sum"],
            "target_chars_max_sum": f["target_chars_max_sum"],
            "target_chars_midpoint_sum": f["target_chars_midpoint_sum"],
            "length_class": dict(f["length_class"]),
            "scenarios_by_type": dict(sorted(types_by_family[name].items())),
        }
    n_mail = unique["mail"]
    n_doc = unique["document"]
    return {
        "schema_version": sk.schema_version,
        "seed": sk.seed,
        "accounts": accounts,
        "scenarios_by_type": dict(sorted(per_type.items())),
        "scenarios_total": len(sk.scenarios),
        "unique_body_specs": {"mail": n_mail, "document": n_doc, "total": n_mail + n_doc},
        "mail_instances": sum(len(a.messages) for a in sk.accounts.values()),
        "length_class": {
            art: {
                "counts": dict(c),
                "ratios": {k: round(v / max(1, sum(c.values())), 4) for k, v in sorted(c.items())},
            }
            for art, c in length.items()
        },
        "generator_family": families,
        "target_chars_midpoint_sum": mid_sum,
        "target_chars_midpoint_sum_by_artifact": dict(mid_by_artifact),
        "all_day_events_total": sum(a["all_day_events"] for a in accounts.values()),
    }


def refs_in(obj: object) -> list[ArtifactRef]:
    """모델·목록·딕셔너리 안의 `ArtifactRef`를 모두 찾는다."""
    found: list[ArtifactRef] = []
    stack: list[object] = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, ArtifactRef):
            found.append(x)
        elif isinstance(x, BaseModel):
            stack.extend(getattr(x, name) for name in type(x).model_fields)
        elif isinstance(x, list | tuple):
            stack.extend(x)
        elif isinstance(x, dict):
            stack.extend(x.values())
    return found
