import random
import uuid
from datetime import UTC, datetime, timedelta

from orbit.core.clock import FixedClock, unix_ms
from orbit.core.ids import Uuid7Generator, uuid7

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def test_version_and_variant_bits() -> None:
    for _ in range(200):
        value = uuid7()
        assert value.version == 7
        assert value.variant == uuid.RFC_4122
        assert (value.int >> 62) & 0b11 == 0b10
        assert (value.int >> 76) & 0xF == 0x7


def test_timestamp_is_first_48_bits() -> None:
    clock = FixedClock(T0)
    value = Uuid7Generator(clock).next()
    assert value.int >> 80 == unix_ms(T0)


def test_orders_by_time() -> None:
    clock = FixedClock(T0)
    gen = Uuid7Generator(clock)
    ids = []
    for _ in range(50):
        ids.append(gen.next())
        clock.advance(timedelta(milliseconds=3))
    assert ids == sorted(ids)
    assert [str(i) for i in ids] == sorted(str(i) for i in ids)


def test_same_millisecond_is_strictly_increasing_and_unique() -> None:
    gen = Uuid7Generator(FixedClock(T0))
    ids = [gen.next() for _ in range(3000)]  # 12비트 카운터(4096)를 넘기도록 충분히 많이
    assert len(set(ids)) == len(ids)
    assert all(a < b for a, b in zip(ids, ids[1:], strict=False))


def test_counter_overflow_borrows_next_millisecond() -> None:
    gen = Uuid7Generator(FixedClock(T0), rand=lambda bits: (1 << bits) - 1 if bits == 11 else 0)
    ids = [gen.next() for _ in range(5000)]
    assert all(a < b for a, b in zip(ids, ids[1:], strict=False))
    assert ids[-1].int >> 80 > unix_ms(T0)


def test_clock_going_backwards_keeps_order() -> None:
    clock = FixedClock(T0)
    gen = Uuid7Generator(clock)
    first = gen.next()
    clock.set(T0 - timedelta(seconds=5))
    second = gen.next()
    assert first < second


def test_seeded_rand_and_fixed_clock_are_reproducible() -> None:
    def make() -> list[uuid.UUID]:
        rng = random.Random(7)  # noqa: S311 - 재현성 테스트용 시드 난수
        gen = Uuid7Generator(FixedClock(T0), rand=rng.getrandbits)
        return [gen.next() for _ in range(10)]

    assert make() == make()
