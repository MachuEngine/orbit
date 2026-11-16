"""뼈대를 Sim 세계 스냅샷(`SimWorld.load`가 읽는 dict)으로 내보낸다.

본문은 `{body_id: 본문}`으로 받는다. 없으면 `template_text`를 쓴다. snippet은 v0와 같은 방식
(공백을 하나로 줄인 본문 앞 60자)으로 만든다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from orbit.evaldata.v1.skeleton import Skeleton


def snippet_of(body: str) -> str:
    return " ".join(body.split())[:60]


def export_world(skeleton: Skeleton, bodies: Mapping[str, str] | None = None) -> dict[str, Any]:
    texts = bodies or {}

    def text(body_id: str) -> str:
        return texts.get(body_id) or skeleton.bodies[body_id].template_text

    accounts: dict[str, Any] = {}
    for acct, a in skeleton.accounts.items():
        messages: list[dict[str, Any]] = []
        for m in a.messages:
            d = m.model_dump(mode="json", by_alias=True)
            body = text(d.pop("body_id"))
            d["body_text"] = body
            d["snippet"] = snippet_of(body)
            d["headers"] = {}
            messages.append(d)
        files: list[dict[str, Any]] = []
        for f in a.files:
            d = f.model_dump(mode="json")
            d["text"] = text(d.pop("body_id"))
            files.append(d)
        accounts[acct] = {
            "email": a.email,
            "calendars": [c.model_dump(mode="json") for c in a.calendars],
            "events": [e.model_dump(mode="json") for e in a.events],
            "messages": messages,
            "threads": [t.model_dump(mode="json") for t in a.threads],
            "drafts": [],
            "files": files,
        }
    return {"schema_version": 1, "seed": skeleton.seed, "accounts": accounts}
