"""색인 범위로 고를 수 있는 라벨 (ADR-0019 §1)."""

import pytest

from orbit.connectors.contracts import MailLabel
from orbit.mailindex.labels import is_indexable_label


def _label(label_id: str, kind: str = "system") -> MailLabel:
    return MailLabel(id=label_id, name=label_id, kind=kind)


@pytest.mark.parametrize(
    "label_id",
    ["INBOX", "SENT", "STARRED", "IMPORTANT", "CATEGORY_UPDATES", "CATEGORY_PROMOTIONS"],
)
def test_ordinary_system_labels_are_indexable(label_id: str) -> None:
    assert is_indexable_label(_label(label_id))


@pytest.mark.parametrize(
    "label_id",
    [
        "UNREAD",
        "DRAFT",
        "SPAM",
        "TRASH",
        "CHAT",
        "YELLOW_STAR",
        "BLUE_STAR",
        "RED_STAR",
        "ORANGE_STAR",
        "RED_BANG",
        "YELLOW_BANG",
        "ORANGE_GUILLEMET",
        "BLUE_INFO",
        "GREEN_CHECK",
        "PURPLE_QUESTION",
    ],
)
def test_state_flag_and_superstar_labels_are_not_indexable(label_id: str) -> None:
    assert not is_indexable_label(_label(label_id))


def test_user_labels_are_always_indexable_even_with_a_system_like_name() -> None:
    assert is_indexable_label(_label("Label_7", "user"))
    assert is_indexable_label(_label("UNREAD", "user"))
