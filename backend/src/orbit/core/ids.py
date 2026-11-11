"""UUIDv7 생성기 (RFC 9562, 표준 라이브러리만 사용). 도메인 ID는 모두 여기서 만든다 (D4-3).

비트 배치: 48비트 Unix ms | 버전 7(4비트) | rand_a 12비트 | variant 10(2비트) | rand_b 62비트.

단조 증가 방식 (RFC 9562 §6.2 방법 1, 고정 길이 전용 카운터):
- `rand_a` 12비트를 같은 ms 안의 카운터로 쓴다. 새 ms의 첫 값은 난수(상위 1비트는 0)로 시작해
  올림 여유를 두고, 같은 ms에서 다시 부르면 1씩 올린다.
- 카운터가 12비트를 넘으면 타임스탬프를 1ms 앞당겨 이어간다.
- 시계가 거꾸로 가도 마지막 타임스탬프보다 작아지지 않게 마지막 값을 유지한다.
- 따라서 같은 생성기에서 나온 값은 바이트 순서로 엄격히 증가한다(유일성도 함께 보장).
- 생성기 하나는 스레드 안전하다(잠금). 프로세스가 다르면 서로 단조성을 보장하지 않는다.
  `rand_b`가 62비트 난수여서 프로세스 사이 충돌 확률은 무시할 수 있다.

평가 재현성: 생성기에 `FixedClock`과 시드 난수 함수(`rand`)를 주입할 수 있다.
"""

import secrets
import threading
import uuid
from collections.abc import Callable

from orbit.core.clock import Clock, SystemClock, unix_ms

_COUNTER_MAX = 0xFFF
_MS_MAX = (1 << 48) - 1


class Uuid7Generator:
    def __init__(
        self,
        clock: Clock | None = None,
        rand: Callable[[int], int] = secrets.randbits,
    ) -> None:
        """`rand(k)`는 k비트 난수 정수를 돌려주는 함수다."""
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._rand = rand
        self._lock = threading.Lock()
        self._last_ms = -1
        self._counter = 0

    def next(self) -> uuid.UUID:
        with self._lock:
            ms = unix_ms(self._clock.now())
            if ms > self._last_ms:
                self._last_ms = ms
                self._counter = self._rand(11)  # 상위 비트는 0으로 두어 올림 여유를 남긴다
            else:
                self._counter += 1
                if self._counter > _COUNTER_MAX:
                    self._last_ms += 1
                    self._counter = self._rand(11)
            if not 0 <= self._last_ms <= _MS_MAX:
                raise ValueError("UUIDv7 타임스탬프 범위(48비트)를 벗어났다")
            value = (
                (self._last_ms << 80)
                | (0x7 << 76)
                | (self._counter << 64)
                | (0b10 << 62)
                | self._rand(62)
            )
        return uuid.UUID(int=value)


_default = Uuid7Generator()


def uuid7(generator: Uuid7Generator | None = None) -> uuid.UUID:
    """UUIDv7을 만든다. 생성기를 주지 않으면 프로세스 공용(시스템 시계) 생성기를 쓴다."""
    return (generator or _default).next()
