"""시계 주입. 도메인 코드는 현재 시각을 `Clock`에서만 얻는다 (D1-5)."""

from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MS = timedelta(milliseconds=1)


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """시간대가 있는(UTC) 현재 시각을 돌려준다."""
        ...


class SystemClock:
    """실제 시각을 돌려준다."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """테스트·평가용 시계. 직접 옮기기 전에는 시각이 변하지 않는다."""

    def __init__(self, at: datetime) -> None:
        self._at = _require_aware(at)

    def now(self) -> datetime:
        return self._at

    def set(self, at: datetime) -> None:
        self._at = _require_aware(at)

    def advance(self, delta: timedelta) -> None:
        self._at = self._at + delta


def unix_ms(moment: datetime) -> int:
    """시각을 Unix epoch 이후 밀리초 정수로 바꾼다(부동소수점 오차 없이)."""
    return (_require_aware(moment) - _EPOCH) // _MS


def _require_aware(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("시간대 없는 datetime은 쓸 수 없다")
    return moment.astimezone(UTC)
