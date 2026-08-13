"""Primitive runtime seams: time and identifier generation.

Injected everywhere rather than called directly, so tests are deterministic and
nothing depends on wall-clock ordering. ``datetime.now()`` in domain or
application code is a bug.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from itertools import count
from uuid import UUID, uuid4

__all__ = [
    "Clock",
    "FixedClock",
    "IdFactory",
    "SequentialIds",
    "system_clock",
    "uuid_factory",
]

Clock = Callable[[], datetime]
IdFactory = Callable[[], UUID]


def system_clock() -> datetime:
    return datetime.now(tz=UTC)


def uuid_factory() -> UUID:
    return uuid4()


class FixedClock:
    """A clock that advances only when asked."""

    def __init__(self, start: datetime, *, step_seconds: int = 1) -> None:
        if start.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._now = start
        self._step = step_seconds

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: int | None = None) -> datetime:
        from datetime import timedelta

        self._now = self._now + timedelta(seconds=seconds if seconds is not None else self._step)
        return self._now


class SequentialIds:
    """Deterministic UUIDs for tests and fixtures.

    Produces ``00000000-0000-4000-8000-0000000000NN`` so failures name a
    recognisable id instead of random noise.
    """

    def __init__(self, prefix: int = 0) -> None:
        self._counter = count(1)
        self._prefix = prefix

    def __call__(self) -> UUID:
        value = next(self._counter)
        return UUID(f"{self._prefix:08x}-0000-4000-8000-{value:012d}")
