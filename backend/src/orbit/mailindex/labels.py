"""색인 범위로 고를 수 있는 라벨 (ADR-0019 §1).

읽음 표시(`UNREAD`)나 별표 변형(superstar: `YELLOW_STAR`, `RED_BANG` 등)은 사용자가 수시로 바꿔
범위로 맞지 않고, 초안·스팸·휴지통·채팅은 색인 대상이 아니다. 사용자 라벨과 나머지 시스템 라벨
(`INBOX`, `SENT`, `STARRED`, `IMPORTANT`, `CATEGORY_*`)은 고를 수 있다.
화면(`frontend/src/connections/text.ts`)도 같은 목록으로 거른다.
"""

from orbit.connectors.contracts import MailLabel

EXCLUDED_SYSTEM_LABELS = frozenset({"DRAFT", "SPAM", "TRASH", "CHAT", "UNREAD"})
SUPERSTAR_SUFFIXES = ("_STAR", "_BANG", "_GUILLEMET", "_INFO", "_CHECK", "_QUESTION")


def is_indexable_label(label: MailLabel) -> bool:
    if label.kind == "user":
        return True
    return label.id not in EXCLUDED_SYSTEM_LABELS and not label.id.endswith(SUPERSTAR_SUFFIXES)
